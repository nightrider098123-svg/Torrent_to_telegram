# Split File Fallback Strategy

This document explains the fallback strategy when uploading a single file that is too large.

When the Telegram Bot API is used for uploads, the standard limit is generally 50 MB (or 20 MB directly, but up to 2 GB for a local bot API server). Because the repository supports MTProto userbot uploads (using Pyrogram), users can upload files seamlessly up to 2 GB (and some Premium configurations support 4 GB).

However, if a single file exceeds the configured `telegram_file_limit_bytes` (which defaults to `2147483648` bytes or 2 GB), it cannot be successfully uploaded to Telegram using a single message via the API limits.

## How it works:
1. **Prefer MTProto:** The system prioritizes using the Pyrogram MTProto userbot upload mechanism for its support of larger file sizes natively.
2. **File Size Check:** Before initiating an upload, the system will verify the size of the file against the `telegram_file_limit_bytes` limit.
3. **Split Fallback:** If the single file size surpasses this limit, it is necessary to split the file into smaller chunks. The system will slice the original large file into multiple parts. Each part will strictly fall under the maximum allowed byte limit.
4. **Sequential Upload:** The resulting file parts are sequentially uploaded into the target dump channel. The channel metadata message will explicitly list the parts in order, letting users know they must combine the file parts to recreate the original file locally (e.g., using a `cat` command like `cat filename.part* > filename`).

This ensures that media or archives which slightly exceed normal bounds can still be pushed to Telegram storage, provided the original file is reconstructed upon download.
