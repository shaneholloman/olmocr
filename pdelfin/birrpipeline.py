import os
import hashlib
import boto3
import sqlite3
import json
import argparse
import glob
import tempfile
import datetime
import posixpath
import threading
import logging

from dataclasses import dataclass
from pypdf import PdfReader
from tqdm import tqdm
from functools import partial
from typing import Optional, List, Tuple, Dict, Callable, Any
from urllib.parse import urlparse
from concurrent.futures import ProcessPoolExecutor, as_completed

from pdelfin.data.renderpdf import render_pdf_to_base64png
from pdelfin.prompts import build_finetuning_prompt
from pdelfin.prompts.anchor import get_anchor_text

# Global s3 client for the whole script, feel free to adjust params if you need it
s3 = boto3.client('s3')

# Quiet logs from pypdf and smart open
logging.getLogger("pypdf").setLevel(logging.ERROR)
logging.getLogger("smart_open").setLevel(logging.ERROR)

class DatabaseManager:
    @dataclass(frozen=True)
    class BatchInferenceRecord:
        inference_s3_path: str
        pdf_s3_path: str
        page_num: int  # 1 indexed!
        round: int
        start_index: int
        length: int
        finish_reason: str
        error: Optional[str]

        def is_usable(self):
            return self.error is None and self.finish_reason == "stop"

    @dataclass(frozen=True)
    class PDFRecord:
        s3_path: str
        num_pages: int
        status: str

    def __init__(self, s3_workspace: str):
        cache_key = hashlib.sha256(s3_workspace.strip().lower().encode('utf-8')).hexdigest()
        home_cache_dir = os.path.join(os.path.expanduser('~'), '.cache', 'pdelfin', cache_key)
        os.makedirs(home_cache_dir, exist_ok=True)
        self.db_path = os.path.join(home_cache_dir, 'index.db')
        
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        self._initialize_tables()

    def _initialize_tables(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS page_results (
                inference_s3_path TEXT,
                pdf_s3_path TEXT,
                page_num INTEGER,
                round INTEGER,
                start_index BIGINT,
                length BIGINT,
                finish_reason TEXT,
                error TEXT
            )
        """)
        self.cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_path ON page_results(pdf_s3_path)
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS pdfs (
                s3_path TEXT PRIMARY KEY,
                num_pages INTEGER,
                status TEXT DEFAULT 'pending'
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_files (
                s3_path TEXT PRIMARY KEY,
                etag TEXT
            )
        """)
        # Generic metadata such as current round
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        self.conn.commit()

    def get_metadata(self, key: str) -> Optional[str]:
        self.cursor.execute("SELECT value FROM metadata WHERE key=?", (key,))
        result = self.cursor.fetchone()
        return result[0] if result else None
    
    def set_metadata(self, key: str, value: str) -> None:
        self.cursor.execute("""
            INSERT INTO metadata (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))
        self.conn.commit()

    def get_current_round(self):
        round_value = self.get_metadata("round")
        return int(round_value) if round_value else 0

    def is_file_processed(self, s3_path, etag):
        self.cursor.execute("SELECT etag FROM processed_files WHERE s3_path = ?", (s3_path,))
        result = self.cursor.fetchone()
        return result is not None and result[0] == etag
    
    def update_processed_file(self, s3_path, etag):
        self.cursor.execute("""
            INSERT INTO processed_files (s3_path, etag)
            VALUES (?, ?)
            ON CONFLICT(s3_path) DO UPDATE SET etag=excluded.etag
        """, (s3_path, etag))
        self.conn.commit()

    def add_index_entries(self, index_entries: List[BatchInferenceRecord]):
        if index_entries:
            self.cursor.executemany("""
                INSERT INTO page_results (inference_s3_path, pdf_s3_path, page_num, round, start_index, length, finish_reason, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [(entry.inference_s3_path, entry.pdf_s3_path, entry.page_num, entry.round, entry.start_index, entry.length, entry.finish_reason, entry.error) for entry in index_entries])
            self.conn.commit()

    def get_index_entries(self, pdf_s3_path: str) -> List[BatchInferenceRecord]:
        self.cursor.execute("""
            SELECT inference_s3_path, pdf_s3_path, page_num, round, start_index, length, finish_reason, error
            FROM page_results
            WHERE pdf_s3_path = ?
            ORDER BY inference_s3_path DESC, start_index ASC, page_num ASC
        """, (pdf_s3_path,))
        
        rows = self.cursor.fetchall()
        
        return [
            self.BatchInferenceRecord(
                inference_s3_path=row[0],
                pdf_s3_path=row[1],
                page_num=row[2],
                round=row[3],
                start_index=row[4],
                length=row[5],
                finish_reason=row[6],
                error=row[7]
            )
            for row in rows
        ]

    def get_last_indexed_round(self) -> int:
        self.cursor.execute("""
            SELECT MAX(round)
            FROM page_results
        """)

        result = self.cursor.fetchone()
        return -1 if result[0] is None else result[0]

    def pdf_exists(self, s3_path: str) -> bool:
        self.cursor.execute("SELECT 1 FROM pdfs WHERE s3_path = ?", (s3_path,))
        return self.cursor.fetchone() is not None

    def add_pdf(self, s3_path: str, num_pages: int, status: str = 'pending') -> None:
        try:
            self.cursor.execute("""
                INSERT INTO pdfs (s3_path, num_pages, status)
                VALUES (?, ?, ?)
            """, (s3_path, num_pages, status))
            self.conn.commit()
        except sqlite3.IntegrityError:
            print(f"PDF with s3_path '{s3_path}' already exists.")

    def update_pdf_status(self, s3_path: str, new_status: str) -> None:
        self.cursor.execute("""
            UPDATE pdfs
            SET status = ?
            WHERE s3_path = ?
        """, (new_status, s3_path))
        self.conn.commit()

    def get_pdf(self, s3_path: str) -> Optional[PDFRecord]:
        self.cursor.execute("""
            SELECT s3_path, num_pages, status
            FROM pdfs
            WHERE s3_path = ?
        """, (s3_path,))
        
        row = self.cursor.fetchone()
        
        if row:
            return self.PDFRecord(
                s3_path=row[0],
                num_pages=row[1],
                status=row[2]
            )
        return None
    
    def get_pdfs_by_status(self, status: str) -> List[PDFRecord]:
        self.cursor.execute("""
            SELECT s3_path, num_pages, status
            FROM pdfs
            WHERE status == ?
            ORDER BY s3_path DESC
        """, (status, ))
        
        rows = self.cursor.fetchall()
        
        return [
            self.PDFRecord(
                s3_path=row[0],
                num_pages=row[1],
                status=row[2]
            )
            for row in rows
        ]

    def close(self):
        self.conn.close()


# Writes batches of lines out to a set of files, keeping each file below some maximum size
class BatchWriter:
    def __init__(self, output_prefix: str, max_size_mb: int = 250, after_flush: Optional[Callable[[List[str]], Any]] = None):
        self.output_prefix = output_prefix
        self.max_size = max_size_mb * 1024 * 1024  # Convert MB to bytes
        self.batch = []
        self.batch_size = 0
        self.after_flush = after_flush
        self.threads = []

        parsed = urlparse(output_prefix)
        self.is_s3 = parsed.scheme in ('s3', 's3a', 's3n')

        if not self.is_s3:
            os.makedirs(output_prefix, exist_ok=True)

    def _compute_hash(self, content: str) -> str:
        """Compute a 20-character SHA1 hash of the given content."""
        sha1 = hashlib.sha1()
        sha1.update(content.encode('utf-8'))
        return sha1.hexdigest()[:20]

    def _get_output_path(self, hash_str: str) -> str:
        """Generate the full output path with hash in the filename."""
        parsed = urlparse(self.output_prefix)
        if self.is_s3:
            bucket = parsed.netloc
            key = parsed.path.lstrip('/')
            if key and not key.endswith('/'):
                key += '/'
            full_key = posixpath.join(key, f"output_{hash_str}.jsonl")
            return f"s3://{bucket}/{full_key}"
        else:
            filename = f"output_{hash_str}.jsonl"
            return os.path.join(self.output_prefix, filename)

    def write_line(self, line: Optional[str]):
        if line is None or not line.strip():
            return

        line_size = len(line.encode('utf-8')) + 1  # +1 for newline

        if self.batch_size + line_size > self.max_size:
            self._write_batch()

        self.batch.append(line)
        self.batch_size += line_size

    def _write_batch(self):
        if not self.batch:
            return

        batch_lines = self.batch.copy()
        batch_content = "\n".join(batch_lines) + "\n"
        hash_str = self._compute_hash(batch_content)
        output_path = self._get_output_path(hash_str)

        # Start a new thread to write the batch
        thread = threading.Thread(
            target=self._write_batch_to_file,
            args=(batch_content, output_path, batch_lines)
        )
        thread.start()
        self.threads.append(thread)

        # Clear the batch and batch_size
        self.batch = []
        self.batch_size = 0

    def _write_batch_to_file(self, batch_content: str, output_path: str, batch_lines: List[str]):
        if self.is_s3:
            self._write_to_s3(batch_content, output_path)
        else:
            with open(output_path, 'w', encoding='utf-8') as f_out:
                f_out.write(batch_content)

        # After writing, call the after_flush callback if it is set
        if self.after_flush:
            self.after_flush(batch_lines)

    def _write_to_s3(self, content: str, s3_path: str):
        # Parse the s3_path to get bucket and key
        parsed = urlparse(s3_path)
        bucket = parsed.netloc
        key = parsed.path.lstrip('/')

        s3 = boto3.client('s3')
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode('utf-8'),
            ContentType='text/plain; charset=utf-8'
        )

    def close(self):
        self._write_batch()
        # Wait for all threads to finish
        for thread in self.threads:
            thread.join()


def parse_s3_path(s3_path):
    if not s3_path.startswith('s3://'):
        raise ValueError('s3_path must start with s3://')
    path = s3_path[5:]
    bucket, _, prefix = path.partition('/')
    return bucket, prefix

def expand_s3_glob(s3_glob: str) -> Dict[str, str]:
    parsed = urlparse(s3_glob)
    bucket_name = parsed.netloc
    prefix = os.path.dirname(parsed.path.lstrip('/')).rstrip('/') + "/"
    pattern = os.path.basename(parsed.path)

    paginator = s3.get_paginator('list_objects_v2')
    page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

    matched_files = {}
    for page in page_iterator:
        for obj in page.get('Contents', []):
            key = obj['Key']
            if glob.fnmatch.fnmatch(key, posixpath.join(prefix, pattern)):
                matched_files[f"s3://{bucket_name}/{key}"] = obj['ETag'].strip('"')

    return matched_files

def build_page_query(local_pdf_path: str, pretty_pdf_path: str, page: int) -> dict:
    image_base64 = render_pdf_to_base64png(local_pdf_path, page, 1024)
    anchor_text = get_anchor_text(local_pdf_path, page, pdf_engine="pdfreport")

    return {
        "custom_id": f"{pretty_pdf_path}-{page}",
        "chat_messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_finetuning_prompt(anchor_text)},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
                ],
            }
        ],
    }


def get_s3_bytes(s3_path: str, start_index: Optional[int] = None, end_index: Optional[int] = None) -> bytes:
    bucket, key = parse_s3_path(s3_path)

    # Build the range header if start_index and/or end_index are specified
    range_header = None
    if start_index is not None and end_index is not None:
        # Range: bytes=start_index-end_index
        range_value = f"bytes={start_index}-{end_index}"
        range_header = {'Range': range_value}
    elif start_index is not None and end_index is None:
        # Range: bytes=start_index-
        range_value = f"bytes={start_index}-"
        range_header = {'Range': range_value}
    elif start_index is None and end_index is not None:
        # Range: bytes=-end_index (last end_index bytes)
        range_value = f"bytes=-{end_index}"
        range_header = {'Range': range_value}

    if range_header:
        obj = s3.get_object(Bucket=bucket, Key=key, Range=range_header['Range'])
    else:
        obj = s3.get_object(Bucket=bucket, Key=key)

    return obj['Body'].read()


def parse_custom_id(custom_id: str) -> Tuple[str, int]:
    s3_path = custom_id[:custom_id.rindex("-")]
    page_num = int(custom_id[custom_id.rindex("-") + 1:])
    return s3_path, page_num

def process_jsonl_content(inference_s3_path: str) -> List[DatabaseManager.BatchInferenceRecord]:
    content_bytes = get_s3_bytes(inference_s3_path)

    start_index = 0
    index_entries = []
    lines = content_bytes.splitlines(keepends=True)  # Split content into lines as bytes
    for line in lines:
        line_length = len(line)  # Length in bytes

        try:
            # Decode the line for JSON processing
            line_str = line.decode('utf-8')
            data = json.loads(line_str)
            pdf_s3_path, page_num = parse_custom_id(data["custom_id"])

            assert "outputs" in data and len(data["outputs"]) > 0, "No outputs from model detected"

            # Try to parse the actual model response JSON
            try:
                model_response_json = json.loads(data["outputs"][0]["text"])

                index_entries.append(DatabaseManager.BatchInferenceRecord(
                    inference_s3_path=inference_s3_path,
                    pdf_s3_path=pdf_s3_path,
                    page_num=page_num,
                    round=data["round"],
                    start_index=start_index,  # Byte offset in the original file
                    length=line_length,       # Length in bytes
                    finish_reason=data["outputs"][0]["finish_reason"],
                    error=data.get("completion_error", None)
                ))
            except json.JSONDecodeError:
                index_entries.append(DatabaseManager.BatchInferenceRecord(
                    inference_s3_path=inference_s3_path,
                    pdf_s3_path=pdf_s3_path,
                    page_num=page_num,
                    round=data["round"],
                    start_index=start_index,  # Byte offset in the original file
                    length=line_length,       # Length in bytes
                    finish_reason=data["outputs"][0]["finish_reason"],
                    error="Could not parse model JSON output",
                ))

        except json.JSONDecodeError:
            print(f"Error with JSON Decoding of infrence in {inference_s3_path}")
            # TODO Maybe this needs to add an index error that this json is bad, but that would mean a birr error
        except Exception as e:
            print(f"Error processing line: {e}")

        start_index += line_length  # Increment by the number of bytes

    return index_entries


def get_pdf_num_pages(s3_path: str) -> Optional[int]:
    try:
        with tempfile.NamedTemporaryFile("wb+", suffix=".pdf") as tf:
            tf.write(get_s3_bytes(s3_path))
            tf.flush()

            reader = PdfReader(tf.name)
            return reader.get_num_pages()
    except Exception as ex:
        print(f"Warning, could not add {s3_path} due to {ex}")

    return None

def build_pdf_queries(s3_workspace: str, pdf: DatabaseManager.PDFRecord) -> list[dict]:
    db = DatabaseManager(s3_workspace)
    cur_round = db.get_current_round()

    existing_pages = db.get_index_entries(pdf.s3_path)
    new_queries = []

    # Shortcut out of downloading the actual PDF
    if set(page.page_num for page in existing_pages if page.is_usable()) == set(range(1, pdf.num_pages + 1)):
        return []

    try:
        with tempfile.NamedTemporaryFile("wb+", suffix=".pdf") as tf:
            tf.write(get_s3_bytes(pdf.s3_path))
            tf.flush()

            for target_page_num in range(1, pdf.num_pages + 1):
                # Is there an existing page that has no error
                if any(page.is_usable() and page.page_num == target_page_num for page in existing_pages):
                    continue

                has_errored_previously = sum(page.page_num == target_page_num for page in existing_pages)

                if has_errored_previously:
                    # TODO For now this just retries the page 3 times, which is nothing special
                    new_queries.append({**build_page_query(tf.name, pdf.s3_path, target_page_num), "round": cur_round})
                    new_queries.append({**build_page_query(tf.name, pdf.s3_path, target_page_num), "round": cur_round})
                    new_queries.append({**build_page_query(tf.name, pdf.s3_path, target_page_num), "round": cur_round})

                    # But you can try to do some fancier things, such as rotating the page, removing the pdf hints all together, etc
                else:
                    new_queries.append({**build_page_query(tf.name, pdf.s3_path, target_page_num), "round": cur_round})
    except Exception as ex:
        print(f"Warning, could not get batch inferences lines for {pdf.s3_path} due to {ex}")

    return new_queries

def build_dolma_doc(s3_workspace: str, pdf: DatabaseManager.PDFRecord) -> Optional[dict]:
    db = DatabaseManager(s3_workspace)
    existing_pages = db.get_index_entries(pdf.s3_path)
    document_text = ""
    last_page_start_index = 0
    pdf_page_spans = []

    for target_page_num in range(1, pdf.num_pages + 1):
        target_pages = [page for page in existing_pages if page.is_usable() and page.page_num == target_page_num]

        if len(target_pages) == 0:
            return None
        
        target_page = target_pages[0]

        target_row = get_s3_bytes(target_page.inference_s3_path,
                                    start_index=target_page.start_index,
                                    end_index=target_page.start_index+target_page.length - 1)
        
        target_data = json.loads(target_row.decode("utf-8"))

        target_output = json.loads(target_data["outputs"][0]["text"])

        if target_output["natural_text"] is not None:
            document_text += target_output["natural_text"] + "\n"

        pdf_page_spans.append([last_page_start_index, len(document_text), target_page_num])
        last_page_start_index = len(document_text)

    metadata = {
            "Source-File": pdf.s3_path,
            "pdf-total-pages": pdf.num_pages,
        }
    id_ = hashlib.sha1(document_text.encode()).hexdigest()

    dolma_doc = {
        "id": id_,
        "text": document_text,
        "source": "pdelfin",
        "added": datetime.datetime.now().strftime("%Y-%m-%d"),
        "created": datetime.datetime.now().strftime("%Y-%m-%d"),
        "metadata": metadata,
        "attributes": {
            "pdf_page_numbers": pdf_page_spans
        }
    }

    return dolma_doc

def mark_pdfs_done(s3_workspace: str, dolma_doc_lines: list[str]):
    db = DatabaseManager(s3_workspace)

    for line in dolma_doc_lines:
        db.update_pdf_status(json.loads(line)["metadata"]["Source-File"], "completed")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Manager for running millions of PDFs through a batch inference pipeline')
    parser.add_argument('workspace', help='The S3 path where work will be done e.g., s3://bucket/prefix/)')
    parser.add_argument('--add_pdfs', help='Glob path to add PDFs (s3) to the workspace', default=None)
    parser.add_argument('--max_size_mb', type=int, default=250, help='Max file size in MB')
    args = parser.parse_args()

    db = DatabaseManager(args.workspace)
    print(f"Loaded db at {db.db_path}")
    print(f"Current round is {db.get_current_round()}\n")

    # One shared executor to rule them all
    executor = ProcessPoolExecutor()

    # If you have new PDFs, step one is to add them to the list
    if args.add_pdfs:
        assert args.add_pdfs.startswith("s3://"), "PDFs must live on s3"
        
        print(f"Querying all PDFs at {args.add_pdfs}")
        
        all_pdfs = expand_s3_glob(args.add_pdfs)
        print(f"Found {len(all_pdfs):,} total pdf paths")

        all_pdfs = [pdf for pdf in all_pdfs if not db.pdf_exists(pdf)]
        print(f"Need to import {len(all_pdfs):,} total new pdf paths")

        future_to_path = {executor.submit(get_pdf_num_pages, s3_path): s3_path for s3_path in all_pdfs}
        for future in tqdm(as_completed(future_to_path), total=len(future_to_path)):
            s3_path = future_to_path[future]
            num_pages = future.result()
            if num_pages and not db.pdf_exists(s3_path):
                db.add_pdf(s3_path, num_pages, "pending")

        print("\n")

    # Now build an index of all the pages that were processed within the workspace so far
    print("Indexing all batch inference sent to this workspace")
    inference_output_paths = expand_s3_glob(f"{args.workspace}/inference_outputs/*.jsonl")

    inference_output_paths = [
        (s3_path, etag) for s3_path, etag in inference_output_paths.items()
        if not db.is_file_processed(s3_path, etag)
    ]

    print(f"Found {len(inference_output_paths):,} new batch inference results to index")
    future_to_path = {executor.submit(process_jsonl_content, s3_path): (s3_path, etag) for s3_path, etag in inference_output_paths}

    for future in tqdm(as_completed(future_to_path), total=len(future_to_path)):
        s3_path, etag = future_to_path[future]
        inference_records = future.result()

        db.add_index_entries(inference_records)
        db.update_processed_file(s3_path, etag=etag)

    # Now query each pdf, if you have all of the pages needed (all pages present, error is null and finish_reason is stop), then you assemble it into a dolma document and output it
    # If you don't have every page, or if you have pages with errors, then you output a new batch of inference items to use
    if db.get_last_indexed_round() < db.get_current_round() - 1:
        print(f"WARNING: No new batch inference results found, you need to run batch inference on {args.workspace}/inference_inputs/round_{db.get_current_round() - 1}")
        potentially_done_pdfs = db.get_pdfs_by_status("pending")
    else:
        print(f"\nCreating batch inference files for new PDFs")
        future_to_path = {executor.submit(build_pdf_queries, args.workspace, pdf): pdf for pdf in db.get_pdfs_by_status("pending")}
        potentially_done_pdfs = []
        lines_written = 0
        new_inference_writer = BatchWriter(f"{args.workspace}/inference_inputs/round_{db.get_current_round()}", args.max_size_mb)

        for future in tqdm(as_completed(future_to_path), total=len(future_to_path)):
            pdf = future_to_path[future]
            inference_lines = future.result()

            if len(inference_lines) == 0:
                potentially_done_pdfs.append(pdf)

            for line in inference_lines:
                lines_written += 1

                if line is not None:
                    new_inference_writer.write_line(json.dumps(line))

        new_inference_writer.close()

        if lines_written > 0:
            print(f"Added {lines_written:,} new batch inference requests")
            db.set_metadata("round", str(db.get_current_round() + 1))

    # Now, finally, assemble any potentially done docs into dolma documents
    print(f"\nAssembling {len(potentially_done_pdfs):,} potentially finished PDFs into Dolma documents at {args.workspace}/output")
    future_to_path = {executor.submit(build_dolma_doc, args.workspace, pdf): pdf for pdf in potentially_done_pdfs}
    new_output_writer = BatchWriter(f"{args.workspace}/output", args.max_size_mb, after_flush=partial(mark_pdfs_done, args.workspace))

    for future in tqdm(as_completed(future_to_path), total=len(future_to_path)):
        pdf = future_to_path[future]
        dolma_doc = future.result()
        
        if dolma_doc is not None:
            new_output_writer.write_line(json.dumps(dolma_doc))

    new_output_writer.close()

    print("\nFinal statistics:")

    # Output the number of PDFs in each status "pending" and "completed"
    pending_pdfs = db.get_pdfs_by_status("pending")
    completed_pdfs = db.get_pdfs_by_status("completed")

    print(f"Pending PDFs: {len(pending_pdfs):,}")
    print(f"Completed PDFs: {len(completed_pdfs):,}")

    # For each round, outputs a report of how many pages were processed, how many had errors, and a breakdown by (error, finish_reason)
    total_rounds = db.get_last_indexed_round() + 1
    for round_num in range(total_rounds):
        db.cursor.execute("""
            SELECT COUNT(*), error, finish_reason
            FROM page_results
            WHERE round = ?
            GROUP BY error, finish_reason
        """, (round_num,))
        
        results = db.cursor.fetchall()
        
        total_pages = sum(count for count, _, _ in results)
        print(f"\nInference Round {round_num} - {total_pages:,} pages processed:")

        for count, error, finish_reason in results:
            error_str = error if error is not None else "None"
            print(f"  (error: {error_str}, finish_reason: {finish_reason}) -> {count:,} pages")

    print("\nWork finished, waiting for all workers to finish cleaning up")
    executor.shutdown(wait=True)
    db.close()
    
    # TODO
    # 2. Have a way to apply basic spam + language filter if you can during add pdfs step
    # 3. For retrying, make it so you retry several times with different sampling parameters
