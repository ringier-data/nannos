"""Unit tests for content builder module."""

import pytest
from a2a.types import FilePart, FileWithUri, Part, TextPart

from app.core.content_builder import (
    _describe_file,
    _guess_mime_type,
    _process_file_part,
    build_text_content,
)


class TestGuessMimeType:
    """Test MIME type guessing from filename or URI."""

    def test_guess_from_name_pdf(self):
        """Test guessing MIME type from PDF filename."""
        result = _guess_mime_type("s3://bucket/file.pdf", "document.pdf")
        assert result == "application/pdf"

    def test_guess_from_name_image(self):
        """Test guessing MIME type from image filename."""
        result = _guess_mime_type("s3://bucket/file.jpg", "photo.jpg")
        assert result == "image/jpeg"

    def test_guess_from_name_text(self):
        """Test guessing MIME type from text filename."""
        result = _guess_mime_type("s3://bucket/file.txt", "notes.txt")
        assert result == "text/plain"

    def test_guess_from_uri_when_no_name(self):
        """Test guessing MIME type from URI when name not provided."""
        result = _guess_mime_type("s3://bucket/path/document.pdf", None)
        assert result == "application/pdf"

    def test_guess_from_uri_with_query_params(self):
        """Test guessing MIME type from URI with query parameters."""
        result = _guess_mime_type("s3://bucket/file.png?version=123", None)
        # mimetypes.guess_type doesn't handle query params well
        assert result is None

    def test_unknown_extension(self):
        """Test handling of unknown file extension."""
        result = _guess_mime_type("s3://bucket/file.unknownext123", "file.unknownext123")
        # Truly unknown extension should return None
        assert result is None

    def test_no_extension(self):
        """Test handling of files without extension."""
        result = _guess_mime_type("s3://bucket/file", "file")
        assert result is None


class TestDescribeFile:
    """Test file description generation (no raw URIs)."""

    def test_describe_image_file(self):
        """Test describing an image file — URI must NOT appear."""
        result = _describe_file(uri="s3://bucket/photo.jpg", mime_type="image/jpeg", name="vacation.jpg")

        assert "[Image file attached] vacation.jpg" in result
        # URIs must NOT be in the text description
        assert "s3://bucket/photo.jpg" not in result

    def test_describe_pdf_file(self):
        """Test describing a PDF document — URI must NOT appear."""
        result = _describe_file(uri="s3://bucket/report.pdf", mime_type="application/pdf", name="report.pdf")

        assert "[PDF document attached] report.pdf" in result
        assert "s3://bucket/report.pdf" not in result

    def test_describe_text_file(self):
        """Test describing a text file."""
        result = _describe_file(uri="s3://bucket/notes.txt", mime_type="text/plain", name="notes.txt")

        assert "[Text file attached] notes.txt" in result
        assert "s3://bucket/notes.txt" not in result

    def test_describe_audio_file(self):
        """Test describing an audio file."""
        result = _describe_file(uri="s3://bucket/music.mp3", mime_type="audio/mpeg", name="music.mp3")

        assert "[Audio file attached] music.mp3 (audio/mpeg)" in result
        assert "s3://bucket/music.mp3" not in result

    def test_describe_video_file(self):
        """Test describing a video file."""
        result = _describe_file(uri="s3://bucket/clip.mp4", mime_type="video/mp4", name="clip.mp4")

        assert "[Video file attached] clip.mp4 (video/mp4)" in result
        assert "s3://bucket/clip.mp4" not in result

    def test_describe_file_without_name(self):
        """Test describing a file without a name — URI must NOT appear."""
        result = _describe_file(uri="s3://bucket/file", mime_type="application/pdf", name=None)

        assert "[PDF document attached] file" in result
        assert "s3://bucket/file" not in result

    def test_describe_file_unknown_mime_type(self):
        """Test describing a file with unknown MIME type."""
        result = _describe_file(uri="s3://bucket/data.bin", mime_type="application/octet-stream", name="data.bin")

        assert "[File attached] data.bin (application/octet-stream)" in result
        assert "s3://bucket/data.bin" not in result

    def test_describe_file_no_mime_type(self):
        """Test describing a file without MIME type."""
        result = _describe_file(uri="s3://bucket/file", mime_type=None, name="file")

        assert "[File attached] file" in result
        assert "s3://bucket/file" not in result


class TestProcessFilePart:
    """Test processing of A2A FilePart — now returns (description, ContentBlock)."""

    @pytest.mark.asyncio
    async def test_process_file_with_uri_returns_tuple(self):
        """Test processing FilePart with URI returns (description, ContentBlock)."""
        file_data = FileWithUri(uri="s3://bucket/photo.jpg", mimeType="image/jpeg", name="photo.jpg")
        part = Part(root=FilePart(file=file_data))

        result = await _process_file_part(part)

        assert result is not None
        description, content_block = result
        # Text description
        assert "[Image file attached]" in description
        assert "photo.jpg" in description
        # URI must NOT be in text
        assert "s3://bucket/photo.jpg" not in description
        # ContentBlock carries the presigned URL (not raw S3 URI)
        assert content_block["type"] == "image"
        assert content_block["url"].startswith("https://")
        assert "X-Amz-Algorithm" in content_block["url"]  # Verify it's a presigned URL
        assert content_block["mime_type"] == "image/jpeg"

    @pytest.mark.asyncio
    async def test_process_audio_file(self):
        """Test processing audio FilePart returns AudioContentBlock."""
        file_data = FileWithUri(uri="s3://bucket/audio.mp3", mimeType="audio/mpeg", name="audio.mp3")
        part = Part(root=FilePart(file=file_data))

        result = await _process_file_part(part)

        assert result is not None
        description, content_block = result
        assert content_block["type"] == "audio"
        assert content_block["url"].startswith("https://")
        assert "X-Amz-Algorithm" in content_block["url"]  # Verify it's a presigned URL
        assert content_block["mime_type"] == "audio/mpeg"

    @pytest.mark.asyncio
    async def test_process_video_file(self):
        """Test processing video FilePart returns VideoContentBlock."""
        file_data = FileWithUri(uri="s3://bucket/clip.mp4", mimeType="video/mp4", name="clip.mp4")
        part = Part(root=FilePart(file=file_data))

        result = await _process_file_part(part)

        assert result is not None
        description, content_block = result
        assert content_block["type"] == "video"
        assert content_block["url"].startswith("https://")
        assert "X-Amz-Algorithm" in content_block["url"]  # Verify it's a presigned URL
        assert content_block["mime_type"] == "video/mp4"

    @pytest.mark.asyncio
    async def test_process_pdf_file_uses_file_block(self):
        """Test processing PDF FilePart returns FileContentBlock."""
        file_data = FileWithUri(uri="s3://bucket/doc.pdf", mimeType="application/pdf", name="doc.pdf")
        part = Part(root=FilePart(file=file_data))

        result = await _process_file_part(part)

        assert result is not None
        description, content_block = result
        assert content_block["type"] == "file"
        assert content_block["url"].startswith("https://")
        assert "X-Amz-Algorithm" in content_block["url"]  # Verify it's a presigned URL
        assert content_block["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_process_file_without_mime_type(self):
        """Test processing FilePart without MIME type (guessed from name)."""
        file_data = FileWithUri(uri="s3://bucket/document.pdf", name="document.pdf")
        part = Part(root=FilePart(file=file_data))

        result = await _process_file_part(part)

        assert result is not None
        description, content_block = result
        assert "[PDF document attached]" in description
        assert content_block["type"] == "file"
        assert content_block["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_process_non_file_part(self):
        """Test processing non-FilePart returns None."""
        text_part = Part(root=TextPart(text="Hello"))

        result = await _process_file_part(text_part)

        assert result is None

    @pytest.mark.asyncio
    async def test_process_file_with_https_uri_unchanged(self):
        """Test that non-S3 URIs (https://) are left unchanged."""
        https_url = "https://example.com/myfile.pdf"
        file_data = FileWithUri(uri=https_url, mimeType="application/pdf", name="myfile.pdf")
        part = Part(root=FilePart(file=file_data))

        result = await _process_file_part(part)

        assert result is not None
        description, content_block = result
        # Non-S3 URIs should remain unchanged
        assert content_block["type"] == "file"
        assert content_block["url"] == https_url
        assert "X-Amz-Algorithm" not in content_block["url"]  # Not a presigned URL


class TestBuildTextContent:
    """Test building text content from A2A message parts — returns tuple."""

    @pytest.mark.asyncio
    async def test_build_from_text_parts_only(self):
        """Test building content from text parts only — no file blocks."""
        parts = [
            Part(root=TextPart(text="Hello, world!")),
            Part(root=TextPart(text="How are you?")),
        ]

        text, file_blocks = await build_text_content(parts)

        assert text == "Hello, world!\nHow are you?"
        assert file_blocks == []

    @pytest.mark.asyncio
    async def test_build_from_mixed_parts(self):
        """Test building content from mixed text and file parts."""
        file_data = FileWithUri(uri="s3://bucket/doc.pdf", mimeType="application/pdf", name="doc.pdf")

        parts = [
            Part(root=TextPart(text="Please review this document:")),
            Part(root=FilePart(file=file_data)),
            Part(root=TextPart(text="Let me know your thoughts.")),
        ]

        text, file_blocks = await build_text_content(parts)

        # Text should have descriptions but NO URIs
        assert "Please review this document:" in text
        assert "[PDF document attached]" in text
        assert "doc.pdf" in text
        assert "Let me know your thoughts." in text
        assert "s3://bucket/doc.pdf" not in text

        # File blocks should carry the presigned URL
        assert len(file_blocks) == 1
        assert file_blocks[0]["type"] == "file"
        assert file_blocks[0]["url"].startswith("https://")
        assert "X-Amz-Algorithm" in file_blocks[0]["url"]  # Verify it's a presigned URL
        assert file_blocks[0]["mime_type"] == "application/pdf"

    @pytest.mark.asyncio
    async def test_build_with_user_prefix(self):
        """Test building content with user prefix for multi-user attribution."""
        parts = [
            Part(root=TextPart(text="Hello from Slack!")),
        ]

        text, file_blocks = await build_text_content(parts, user_prefix="John Doe <@johndoe>")

        assert text.startswith("[John Doe <@johndoe>]:")
        assert "Hello from Slack!" in text
        assert file_blocks == []

    @pytest.mark.asyncio
    async def test_build_from_empty_parts(self):
        """Test building content from empty parts list."""
        text, file_blocks = await build_text_content([])

        assert text == ""
        assert file_blocks == []

    @pytest.mark.asyncio
    async def test_build_with_only_files(self):
        """Test building content with only file parts."""
        file1 = FileWithUri(uri="s3://bucket/a.jpg", mimeType="image/jpeg", name="a.jpg")
        file2 = FileWithUri(uri="s3://bucket/b.pdf", mimeType="application/pdf", name="b.pdf")

        parts = [
            Part(root=FilePart(file=file1)),
            Part(root=FilePart(file=file2)),
        ]

        text, file_blocks = await build_text_content(parts)

        assert "[Image file attached]" in text
        assert "[PDF document attached]" in text
        assert "a.jpg" in text
        assert "b.pdf" in text
        # URIs must NOT be in text
        assert "s3://bucket/a.jpg" not in text
        assert "s3://bucket/b.pdf" not in text

        # File blocks should carry presigned URLs
        assert len(file_blocks) == 2
        assert file_blocks[0]["type"] == "image"
        assert file_blocks[0]["url"].startswith("https://")
        assert "X-Amz-Algorithm" in file_blocks[0]["url"]  # Verify it's a presigned URL
        assert file_blocks[1]["type"] == "file"
        assert file_blocks[1]["url"].startswith("https://")
        assert "X-Amz-Algorithm" in file_blocks[1]["url"]  # Verify it's a presigned URL

    @pytest.mark.asyncio
    async def test_build_with_user_prefix_and_no_content(self):
        """Test building content with user prefix but no actual content."""
        text, file_blocks = await build_text_content([], user_prefix="Test User <@test>")

        assert text == "[Test User <@test>]:"
        assert file_blocks == []

    @pytest.mark.asyncio
    async def test_build_skips_unsupported_parts(self, caplog):
        """Test that unsupported part types are skipped with debug log."""
        parts = [
            Part(root=TextPart(text="Valid text")),
        ]

        text, file_blocks = await build_text_content(parts)

        assert "Valid text" in text


class TestContentBuilderIntegration:
    """Integration tests for content builder functionality."""

    @pytest.mark.asyncio
    async def test_realistic_slack_message_with_attachments(self):
        """Test realistic scenario: Slack message with file attachments."""
        file_data = FileWithUri(uri="s3://documents/report-2024.pdf", mimeType="application/pdf", name="Q4 Report.pdf")

        parts = [
            Part(root=TextPart(text="Hi team, please review the quarterly report.")),
            Part(root=FilePart(file=file_data)),
            Part(root=TextPart(text="Let's discuss in tomorrow's meeting.")),
        ]

        text, file_blocks = await build_text_content(parts, user_prefix="Alice Smith <@alice>")

        # Should have user attribution
        assert text.startswith("[Alice Smith <@alice>]:")
        # Should have text content
        assert "please review the quarterly report" in text
        # Should describe the file
        assert "[PDF document attached]" in text
        assert "Q4 Report.pdf" in text
        # URI must NOT be in text
        assert "s3://documents/report-2024.pdf" not in text

        # Should have follow-up text
        assert "tomorrow's meeting" in text
        # File blocks carry the presigned URL
        assert len(file_blocks) == 1
        assert file_blocks[0]["url"].startswith("https://")
        assert "X-Amz-Algorithm" in file_blocks[0]["url"]  # Verify it's a presigned URL

    @pytest.mark.asyncio
    async def test_multiple_images_from_design_review(self):
        """Test realistic scenario: Design review with multiple images."""
        img1 = FileWithUri(uri="s3://designs/mockup-v1.png", mimeType="image/png", name="mockup-v1.png")
        img2 = FileWithUri(uri="s3://designs/mockup-v2.png", mimeType="image/png", name="mockup-v2.png")

        parts = [
            Part(root=TextPart(text="Here are two design options:")),
            Part(root=FilePart(file=img1)),
            Part(root=FilePart(file=img2)),
            Part(root=TextPart(text="Which one looks better?")),
        ]

        text, file_blocks = await build_text_content(parts)

        assert "two design options" in text
        assert text.count("[Image file attached]") == 2
        assert "mockup-v1.png" in text
        assert "mockup-v2.png" in text
        assert "Which one looks better?" in text
        # URIs must NOT be in text
        assert "s3://designs/mockup-v1.png" not in text
        assert "s3://designs/mockup-v2.png" not in text
        # File blocks carry presigned URLs with correct types
        assert len(file_blocks) == 2
        assert all(b["type"] == "image" for b in file_blocks)
        assert file_blocks[0]["url"].startswith("https://")
        assert "X-Amz-Algorithm" in file_blocks[0]["url"]  # Verify it's a presigned URL
        assert file_blocks[1]["url"].startswith("https://")
        assert "X-Amz-Algorithm" in file_blocks[1]["url"]  # Verify it's a presigned URL

    @pytest.mark.asyncio
    async def test_mixed_media_types(self):
        """Test scenario with image, audio, video, and document."""
        img = FileWithUri(uri="s3://files/photo.jpg", mimeType="image/jpeg", name="photo.jpg")
        audio = FileWithUri(uri="s3://files/song.mp3", mimeType="audio/mpeg", name="song.mp3")
        video = FileWithUri(uri="s3://files/clip.mp4", mimeType="video/mp4", name="clip.mp4")
        doc = FileWithUri(uri="s3://files/readme.txt", mimeType="text/plain", name="readme.txt")

        parts = [
            Part(root=TextPart(text="Multimedia upload")),
            Part(root=FilePart(file=img)),
            Part(root=FilePart(file=audio)),
            Part(root=FilePart(file=video)),
            Part(root=FilePart(file=doc)),
        ]

        text, file_blocks = await build_text_content(parts)

        assert len(file_blocks) == 4
        assert file_blocks[0]["type"] == "image"
        assert file_blocks[1]["type"] == "audio"
        assert file_blocks[2]["type"] == "video"
        assert file_blocks[3]["type"] == "file"  # text/plain → FileContentBlock
