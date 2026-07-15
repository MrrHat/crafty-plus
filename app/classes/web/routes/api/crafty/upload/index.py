import os
import logging
import shutil
import asyncio
import pathlib
from pathlib import Path
import anyio
from PIL import Image
from app.classes.models.server_permissions import EnumPermissionsServer
from app.classes.web.base_api_handler import BaseApiHandler
from app.classes.web.websocket_handler import WebSocketManager

logger = logging.getLogger(__name__)
IMAGE_MIME_TYPES = [
    "image/bmp",
    "image/cis-cod",
    "image/gif",
    "image/ief",
    "image/jpeg",
    "image/pipeg",
    "image/svg+xml",
    "image/tiff",
    "image/x-cmu-raster",
    "image/x-cmx",
    "image/x-icon",
    "image/x-portable-anymap",
    "image/x-portable-bitmap",
    "image/x-portable-graymap",
    "image/x-portable-pixmap",
    "image/x-rgb",
    "image/x-xbitmap",
    "image/x-xpixmap",
    "image/x-xwindowdump",
    "image/png",
    "image/webp",
]

ARCHIVE_MIME_TYPES = [
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
]

CUSTOM_GRAPHICS = "app/frontend/static/assets/images/auth/custom"
FAILED_MSG = "Failed to upload files with error: %s"


class ApiFilesUploadHandler(BaseApiHandler):
    upload_locks = {}

    def get_lock(self, key: str) -> asyncio.Lock:
        """Get or create a lock for the given key."""
        if key not in self.upload_locks:
            self.upload_locks[key] = asyncio.Lock()
        return self.upload_locks[key]

    def check_traversal(self, upload_type: str, **kwargs):
        """Matches upload type and checks default upload location for traversal"""
        if not self.filename:
            raise ValueError

        match upload_type:
            case "server_upload":
                server_path = Path(
                    self.controller.management.get_master_server_dir(),
                    kwargs.get("server_id"),
                )
                self.upload_dir = pathlib.Path(
                    self.file_helper.get_absolute_path(
                        str(server_path), str(Path(server_path, self.location))
                    )
                ).resolve()
                base_dir = Path(
                    self.controller.management.get_master_server_dir(),
                    kwargs.get("server_id"),
                ).resolve()
                self.helper.validate_traversal(
                    base_dir, Path(self.upload_dir, self.filename).resolve()
                )
            case "import":
                self.upload_dir = Path(self.controller.project_root, "import", "upload")
                self.helper.validate_traversal(
                    Path(self.controller.project_root, "import", "upload").resolve(),
                    Path(
                        self.controller.project_root, "import", "upload", self.filename
                    ).resolve(),
                )
            case "background":
                self.upload_dir = os.path.join(
                    self.controller.project_root, CUSTOM_GRAPHICS
                )
                self.helper.validate_traversal(
                    Path(self.controller.project_root, CUSTOM_GRAPHICS).resolve(),
                    Path(self.upload_dir, self.filename).resolve(),
                )
            case _:
                raise ValueError("No suitable upload type found.")

    async def post(self, server_id=None):
        auth_data = self.authenticate_user()
        if not auth_data:
            return

        upload_type = self.request.headers.get("type")

        # 1. Authorize user and resolve types
        auth_result = self._authorize_upload(auth_data, server_id, upload_type)
        if not auth_result:
            return self._finish_unauthorized(auth_data)
        u_type, accepted_types = auth_result

        # 2. Extract and validate headers/paths
        if not self._parse_and_validate_request(
            server_id, upload_type, accepted_types, u_type
        ):
            return

        # 3. Check disk space
        file_size = int(self.request.headers.get("fileSize", 0))
        total_chunks = int(self.request.headers.get("totalChunks", 0))
        if not self._has_enough_space(file_size):
            return self.finish_json(
                507,
                {
                    "status": "error",
                    "error": "NO STORAGE SPACE",
                    "data": {"message": "Out Of Space!"},
                },
            )

        # 4. Handle early chunked initiation or structure prep
        if self.chunked and not self.chunk_index:
            return self.finish_json(
                200, {"status": "ok", "data": {"file-id": self.file_id}}
            )

        os.makedirs(self.upload_dir, exist_ok=True)

        # 5. Route to specific processor
        if not self.chunked:
            return await self._process_non_chunked(u_type)

        return await self._process_chunked(u_type, total_chunks, auth_data, server_id)

    def _authorize_upload(self, auth_data, server_id, upload_type):
        """Determines if user is authorized and returns (u_type, accepted_types)."""
        if server_id:
            if server_id not in [str(x["server_id"]) for x in auth_data[0]]:
                return None
            mask = self.controller.server_perms.get_lowest_api_perm_mask(
                self.controller.server_perms.get_user_permissions_mask(
                    auth_data[4]["user_id"], server_id
                ),
                auth_data[5],
            )
            if (
                EnumPermissionsServer.FILES
                not in self.controller.server_perms.get_permissions(mask)
            ):
                return None
            return "server_upload", []

        if auth_data[4]["superuser"] and upload_type == "background":
            return "admin_config", IMAGE_MIME_TYPES

        if upload_type == "import":
            can_create = self.controller.crafty_perms.can_create_server(
                auth_data[4]["user_id"]
            )
            if can_create or auth_data[4]["superuser"]:
                return "server_import", ARCHIVE_MIME_TYPES

        return None

    def _finish_unauthorized(self, auth_data):
        return self.finish_json(
            400,
            {
                "status": "error",
                "error": "NOT_AUTHORIZED",
                "error_data": self.helper.translation.translate(
                    "validators", "insufficientPerms", auth_data[4]["lang"]
                ),
            },
        )

    def _parse_and_validate_request(
        self, server_id, upload_type, accepted_types, u_type
    ) -> bool:
        """Parses headers and runs path traversal validations.
        Returns False if error response sent."""
        self.chunk_hash = self.request.headers.get("chunkHash", 0)
        self.file_id = self.request.headers.get("fileId")
        self.chunked = self.request.headers.get("chunked", False)
        self.filename = self.request.headers.get("fileName", None)
        self.location = self.request.headers.get("location", None)
        self.chunk_index = self.request.headers.get("chunkId")
        self.temp_dir = Path(self.controller.project_root, "temp", self.file_id)

        try:
            self.check_traversal(upload_type, server_id=server_id)
            self.helper.validate_traversal(
                Path(self.controller.project_root, "temp").resolve(),
                self.temp_dir.resolve(),
            )
        except ValueError as why:
            logger.exception(FAILED_MSG, str(why))
            self.finish_json(
                400, {"status": "error", "error": "BAD REQUEST", "error_data": str(why)}
            )
            return False

        if (
            u_type != "server_upload"
            and self.file_helper.check_mime_types(self.filename) not in accepted_types
        ):
            self.finish_json(
                422,
                {
                    "status": "error",
                    "error": "INVALID FILE TYPE",
                    "data": {
                        "message": f"Invalid File Type only accepts {accepted_types}"
                    },
                },
            )
            return False

        return True

    def _has_enough_space(self, file_size: int) -> bool:
        _, _, free = shutil.disk_usage(self.upload_dir)
        return free > file_size

    async def _process_non_chunked(self, u_type: str):
        """Directly writes complete files."""
        file_path = os.path.join(self.upload_dir, self.filename)
        async with await anyio.open_file(file_path, "wb") as file:
            chunk = self.request.body
            if chunk:
                await file.write(chunk)

        logger.info(f"File upload completed. Filename: {self.filename} Type: {u_type}")
        return self.finish_json(
            200,
            {"status": "completed", "data": {"message": "File uploaded successfully"}},
        )

    async def _process_chunked(
        self, u_type: str, total_chunks: int, auth_data, server_id
    ):
        """Processes incoming file chunks, validates integrity, and merges them."""
        os.makedirs(self.temp_dir, exist_ok=True)
        content_length = int(self.request.headers.get("Content-Length", 0))

        if content_length <= 0 or not self.filename or self.chunk_index is None:
            logger.error(
                "File upload failed. Filename: %s Type: %s Error: Validation Failed",
                self.filename,
                u_type,
            )
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "BAD_REQUEST",
                    "data": {"message": "Invalid request parameters"},
                },
            )

        calculated_hash = self.helper.crypto_helper.calculate_buffer_hash(
            self.request.body
        )
        if str(self.chunk_hash) != str(calculated_hash):
            logger.error(
                "File upload failed. Filename: %s Type: %s Error: INVALID HASH",
                self.filename,
                u_type,
            )
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "INVALID_HASH",
                    "data": {
                        "message": "Hash received does not match reported sent hash.",
                        "chunk_id": self.chunk_index,
                    },
                },
            )

        file_path = Path(self.upload_dir, self.filename)
        chunk_path = Path(self.temp_dir, f"{self.filename}.part{self.chunk_index}")

        try:
            self.helper.validate_traversal(
                Path(self.temp_dir).resolve(), chunk_path.resolve()
            )
        except ValueError as why:
            logger.exception(FAILED_MSG, str(why))
            return self.finish_json(
                400, {"status": "error", "error": "BAD REQUEST", "error_data": str(why)}
            )

        async with self.get_lock(self.file_id):
            async with await anyio.open_file(chunk_path, "wb") as f:
                await f.write(self.request.body)

            received_chunks = [
                f
                for f in os.listdir(self.temp_dir)
                if f.startswith(f"{self.filename}.part")
            ]

            if len(received_chunks) == total_chunks:
                await self._assemble_chunks(file_path, total_chunks, auth_data, u_type)
                self._cleanup_temp_resources()

                if u_type == "admin_config":  # background upload_type
                    self._strip_exif(file_path)

                logger.info(
                    "File upload completed. Filename: %s Path: %s Type: %s",
                    self.filename,
                    file_path,
                    u_type,
                )
                self.controller.management.add_to_audit_log(
                    auth_data[4]["user_id"],
                    f"Uploaded file {self.filename}",
                    server_id,
                    self.get_remote_ip(),
                )
                return self.finish_json(
                    200,
                    {
                        "status": "completed",
                        "data": {"message": "File uploaded successfully"},
                    },
                )

            return self.finish_json(
                200,
                {
                    "status": "partial",
                    "data": {"message": f"Chunk {self.chunk_index} received"},
                },
            )

    async def _assemble_chunks(self, file_path, total_chunks, auth_data, u_type):
        """Stitches all file pieces back together sequentially."""
        async with await anyio.open_file(file_path, "wb") as outfile:
            for i in range(total_chunks):
                WebSocketManager().broadcast_user(
                    auth_data[4]["user_id"],
                    "upload_process",
                    {
                        "cur_file": i,
                        "total_files": total_chunks,
                        "type": u_type,
                        "file_id": self.file_id,
                    },
                )
                chunk_file = os.path.join(self.temp_dir, f"{self.filename}.part{i}")
                async with await anyio.open_file(chunk_file, "rb") as infile:
                    await outfile.write(await infile.read())
                try:
                    await anyio.Path(chunk_file).unlink(missing_ok=True)
                except OSError as why:
                    logger.exception("Failed to remove chunk file with error: %s", why)

    def _cleanup_temp_resources(self):
        try:
            self.file_helper.del_dirs(self.temp_dir)
        except OSError as why:
            logger.exception("Failed to remove temp dir with error: %s", why)

    def _strip_exif(self, image_path):
        """Removes EXIF metadata from uploaded imagery."""
        logger.debug("Stripping exif data from image")
        with Image.open(image_path) as image:
            image_data = list(image.getdata())
            image_no_exif = Image.new(image.mode, image.size)
            image_no_exif.putdata(image_data)
            image_no_exif.save(image_path)
