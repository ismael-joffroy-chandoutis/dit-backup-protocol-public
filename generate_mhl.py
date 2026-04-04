#!/usr/bin/env python3
"""
ASC MHL manifest generator for the backup dashboard.

Usage:
    python generate_mhl.py /Volumes/NavTGV1/A020_BRAW_2026-04-03

Generates an ASC MHL manifest (xxh128) for a folder.
If a precomputed hash file exists at /tmp/{foldername}_xxh128.txt,
those hashes are used instead of re-reading files from disk.

The MHL history is stored in {folder}/ascmhl/ per the ASC MHL spec.
"""

import datetime
import os
import platform
import sys

# Compatibility: ascmhl 1.0.1 uses datetime.UTC which requires Python 3.11+.
# Patch it for Python 3.10.
if not hasattr(datetime, "UTC"):
    datetime.UTC = datetime.timezone.utc

from ascmhl.__version__ import (
    ascmhl_folder_name,
    ascmhl_file_extension,
    ascmhl_tool_name,
    ascmhl_tool_version,
)
from ascmhl.generator import MHLGenerationCreationSession
from ascmhl.hashlist import (
    MHLAuthor,
    MHLCreatorInfo,
    MHLProcess,
    MHLProcessInfo,
    MHLTool,
)
from ascmhl.hasher import hash_file, DirectoryHashContext
from ascmhl.history import MHLHistory
from ascmhl.ignore import MHLIgnoreSpec
from ascmhl.traverse import post_order_lexicographic
from ascmhl import logger, utils


HASH_FORMAT = "xxh128"


def parse_xxhsum_file(hash_file_path, folder_path):
    """Parse an xxhsum-format hash file into a dict {absolute_path: hash_string}.

    Supports two common formats:
      - xxhsum output:  <hash>  <filepath>
      - space-separated: <hash> <filepath>
    """
    hashes = {}
    with open(hash_file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            hash_string = parts[0].lower()
            file_path = parts[1].strip()
            # Resolve relative paths against folder_path
            if not os.path.isabs(file_path):
                file_path = os.path.join(folder_path, file_path)
            file_path = os.path.normpath(file_path)
            hashes[file_path] = hash_string
    return hashes


def generate_mhl(folder_path, author_name="Backup Dashboard", author_role="DIT"):
    """Generate an ASC MHL manifest for folder_path using xxh128.

    Returns the path to the created .mhl file, or raises on error.
    """
    folder_path = os.path.abspath(folder_path)
    if not os.path.isdir(folder_path):
        raise FileNotFoundError(f"Folder not found: {folder_path}")

    folder_name = os.path.basename(folder_path)

    # Check for precomputed hashes at /tmp/{foldername}_xxh128.txt
    precomputed_path = f"/tmp/{folder_name}_xxh128.txt"
    precomputed = {}
    if os.path.exists(precomputed_path):
        precomputed = parse_xxhsum_file(precomputed_path, folder_path)
        print(f"[MHL] Loaded {len(precomputed)} precomputed hashes from {precomputed_path}")

    # Load or initialize the MHL history for this folder
    existing_history = MHLHistory.load_from_path(folder_path)

    # Create ignore spec (honor any existing .mhlignore patterns)
    ignore_spec = MHLIgnoreSpec(existing_history.latest_ignore_patterns())

    # Start a generation creation session
    session = MHLGenerationCreationSession(existing_history, ignore_spec)

    num_hashed = 0
    num_precomputed = 0
    dir_content_hash_mapping = {}
    dir_structure_hash_mapping = {}

    for folder_iter, children in post_order_lexicographic(
        folder_path, session.ignore_spec.get_path_spec()
    ):
        dir_hash_context = DirectoryHashContext(HASH_FORMAT)

        for item_name, is_dir in children:
            file_path = os.path.join(folder_iter, item_name)

            if is_dir:
                # Fold child directory hashes into parent
                child_content = dir_content_hash_mapping.pop(file_path)
                child_structure = dir_structure_hash_mapping.pop(file_path)
                dir_hash_context.append_directory_hashes(
                    file_path, child_content, child_structure
                )
            else:
                # Get hash: precomputed or compute from disk
                norm_path = os.path.normpath(file_path)
                if norm_path in precomputed:
                    hash_string = precomputed[norm_path]
                    num_precomputed += 1
                else:
                    hash_string = hash_file(file_path, HASH_FORMAT)
                    num_hashed += 1

                # Record in session (handles verification against previous generations)
                file_size = os.path.getsize(file_path)
                file_mod_date = datetime.datetime.fromtimestamp(
                    os.path.getmtime(file_path)
                )
                session.append_file_hash(
                    file_path, file_size, file_mod_date, HASH_FORMAT, hash_string
                )

                # Feed directory hash context
                dir_hash_context.append_file_hash(file_path, hash_string)

        # Calculate directory hashes for this level
        content_hash = dir_hash_context.final_content_hash_str()
        structure_hash = dir_hash_context.final_structure_hash_str()
        dir_content_hash_mapping[folder_iter] = content_hash
        dir_structure_hash_mapping[folder_iter] = structure_hash

        modification_date = datetime.datetime.fromtimestamp(
            os.path.getmtime(folder_iter)
        )
        session.append_multiple_format_directory_hashes(
            folder_iter,
            modification_date,
            {HASH_FORMAT: content_hash},
            {HASH_FORMAT: structure_hash},
        )

    # Commit the generation
    creator_info = MHLCreatorInfo()
    creator_info.tool = MHLTool(ascmhl_tool_name, ascmhl_tool_version)
    creator_info.creation_date = utils.datetime_now_isostring()
    creator_info.host_name = platform.node()
    creator_info.comment = "Generated by Backup Dashboard"
    author = MHLAuthor(author_name, role=author_role)
    creator_info.authors.append(author)

    process_info = MHLProcessInfo()
    process_info.process = MHLProcess("in-place")

    session.commit(creator_info, process_info)

    # Find the created .mhl file (ascmhl_folder_name = "ascmhl")
    asc_mhl_dir = os.path.join(folder_path, ascmhl_folder_name)
    mhl_files = sorted(
        [f for f in os.listdir(asc_mhl_dir) if f.endswith(ascmhl_file_extension)],
        key=lambda f: os.path.getmtime(os.path.join(asc_mhl_dir, f)),
    )
    created_file = os.path.join(asc_mhl_dir, mhl_files[-1]) if mhl_files else None

    print(f"[MHL] Done: {num_hashed} hashed from disk, {num_precomputed} from precomputed")
    print(f"[MHL] Generation saved: {created_file}")

    return created_file


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <folder_path>")
        sys.exit(1)

    folder = sys.argv[1]
    try:
        result = generate_mhl(folder)
        if result:
            print(f"MHL manifest: {result}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
