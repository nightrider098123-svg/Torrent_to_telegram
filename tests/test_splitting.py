import os
import tempfile
from utils import split_file_for_telegram

def test_chunk_splitting():
    # Create a 3MB dummy file
    with tempfile.NamedTemporaryFile(delete=False) as f:
        file_path = f.name
        f.write(os.urandom(3 * 1024 * 1024))

    # Split with 1MB max chunk size
    chunks = split_file_for_telegram(file_path, max_chunk_size=1 * 1024 * 1024)

    # Should create 3 chunks exactly
    assert len(chunks) == 3

    # Check sizes
    for i, chunk in enumerate(chunks):
        size = os.path.getsize(chunk)
        if i < 2:
            assert size == 1 * 1024 * 1024
        else:
            assert size <= 1 * 1024 * 1024

    # Cleanup
    os.remove(file_path)
    for chunk in chunks:
        os.remove(chunk)

def test_no_split_needed():
    # Create a 500KB dummy file
    with tempfile.NamedTemporaryFile(delete=False) as f:
        file_path = f.name
        f.write(os.urandom(500 * 1024))

    # Split with 1MB max chunk size
    chunks = split_file_for_telegram(file_path, max_chunk_size=1 * 1024 * 1024)

    # Should return original file path
    assert len(chunks) == 1
    assert chunks[0] == file_path

    # Cleanup
    os.remove(file_path)
