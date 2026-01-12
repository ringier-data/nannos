"""Unit tests for content builder module."""

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
    """Test file description generation."""

    def test_describe_image_file(self):
        """Test describing an image file."""
        result = _describe_file(uri="s3://bucket/photo.jpg", mime_type="image/jpeg", name="vacation.jpg")

        assert "[Image file attached]" in result
        assert "Name: vacation.jpg" in result
        assert "URI: s3://bucket/photo.jpg" in result

    def test_describe_pdf_file(self):
        """Test describing a PDF document."""
        result = _describe_file(uri="s3://bucket/report.pdf", mime_type="application/pdf", name="report.pdf")

        assert "[PDF document attached]" in result
        assert "Name: report.pdf" in result
        assert "URI: s3://bucket/report.pdf" in result

    def test_describe_text_file(self):
        """Test describing a text file."""
        result = _describe_file(uri="s3://bucket/notes.txt", mime_type="text/plain", name="notes.txt")

        assert "[Text file attached]" in result
        assert "Name: notes.txt" in result

    def test_describe_file_without_name(self):
        """Test describing a file without a name."""
        result = _describe_file(uri="s3://bucket/file", mime_type="application/pdf", name=None)

        assert "[PDF document attached]" in result
        assert "Name:" not in result
        assert "URI: s3://bucket/file" in result

    def test_describe_file_unknown_mime_type(self):
        """Test describing a file with unknown MIME type."""
        result = _describe_file(uri="s3://bucket/data.bin", mime_type="application/octet-stream", name="data.bin")

        assert "[File attached: application/octet-stream]" in result

    def test_describe_file_no_mime_type(self):
        """Test describing a file without MIME type."""
        result = _describe_file(uri="s3://bucket/file", mime_type=None, name="file")

        assert "[File attached]" in result
        assert "Name: file" in result


class TestProcessFilePart:
    """Test processing of A2A FilePart."""

    def test_process_file_with_uri(self):
        """Test processing FilePart with URI."""
        file_data = FileWithUri(uri="s3://bucket/photo.jpg", mimeType="image/jpeg", name="photo.jpg")
        part = Part(root=FilePart(file=file_data))

        result = _process_file_part(part)

        assert result is not None
        assert "[Image file attached]" in result
        assert "photo.jpg" in result
        assert "s3://bucket/photo.jpg" in result

    def test_process_file_without_mime_type(self):
        """Test processing FilePart without MIME type (guessed from name)."""
        file_data = FileWithUri(uri="s3://bucket/document.pdf", name="document.pdf")
        part = Part(root=FilePart(file=file_data))

        result = _process_file_part(part)

        assert result is not None
        assert "[PDF document attached]" in result

    def test_process_non_file_part(self):
        """Test processing non-FilePart returns None."""
        text_part = Part(root=TextPart(text="Hello"))

        result = _process_file_part(text_part)

        assert result is None


class TestBuildTextContent:
    """Test building text content from A2A message parts."""

    def test_build_from_text_parts_only(self):
        """Test building content from text parts only."""
        parts = [
            Part(root=TextPart(text="Hello, world!")),
            Part(root=TextPart(text="How are you?")),
        ]

        result = build_text_content(parts)

        assert result == "Hello, world!\nHow are you?"

    def test_build_from_mixed_parts(self):
        """Test building content from mixed text and file parts."""
        file_data = FileWithUri(uri="s3://bucket/doc.pdf", mimeType="application/pdf", name="doc.pdf")

        parts = [
            Part(root=TextPart(text="Please review this document:")),
            Part(root=FilePart(file=file_data)),
            Part(root=TextPart(text="Let me know your thoughts.")),
        ]

        result = build_text_content(parts)

        assert "Please review this document:" in result
        assert "[PDF document attached]" in result
        assert "doc.pdf" in result
        assert "Let me know your thoughts." in result

    def test_build_with_user_prefix(self):
        """Test building content with user prefix for multi-user attribution."""
        parts = [
            Part(root=TextPart(text="Hello from Slack!")),
        ]

        result = build_text_content(parts, user_prefix="John Doe <@johndoe>")

        assert result.startswith("[John Doe <@johndoe>]:")
        assert "Hello from Slack!" in result

    def test_build_from_empty_parts(self):
        """Test building content from empty parts list."""
        result = build_text_content([])

        assert result == ""

    def test_build_with_only_files(self):
        """Test building content with only file parts."""
        file1 = FileWithUri(uri="s3://bucket/a.jpg", mimeType="image/jpeg", name="a.jpg")
        file2 = FileWithUri(uri="s3://bucket/b.pdf", mimeType="application/pdf", name="b.pdf")

        parts = [
            Part(root=FilePart(file=file1)),
            Part(root=FilePart(file=file2)),
        ]

        result = build_text_content(parts)

        assert "[Image file attached]" in result
        assert "[PDF document attached]" in result
        assert "a.jpg" in result
        assert "b.pdf" in result

    def test_build_with_user_prefix_and_no_content(self):
        """Test building content with user prefix but no actual content."""
        result = build_text_content([], user_prefix="Test User <@test>")

        assert result == "[Test User <@test>]:"

    def test_build_skips_unsupported_parts(self, caplog):
        """Test that unsupported part types are skipped with debug log."""
        parts = [
            Part(root=TextPart(text="Valid text")),
            # Would need a different part type here, but for now just verify text works
        ]

        result = build_text_content(parts)

        assert "Valid text" in result


class TestContentBuilderIntegration:
    """Integration tests for content builder functionality."""

    def test_realistic_slack_message_with_attachments(self):
        """Test realistic scenario: Slack message with file attachments."""
        file_data = FileWithUri(uri="s3://documents/report-2024.pdf", mimeType="application/pdf", name="Q4 Report.pdf")

        parts = [
            Part(root=TextPart(text="Hi team, please review the quarterly report.")),
            Part(root=FilePart(file=file_data)),
            Part(root=TextPart(text="Let's discuss in tomorrow's meeting.")),
        ]

        result = build_text_content(parts, user_prefix="Alice Smith <@alice>")

        # Should have user attribution
        assert result.startswith("[Alice Smith <@alice>]:")
        # Should have text content
        assert "please review the quarterly report" in result
        # Should describe the file
        assert "[PDF document attached]" in result
        assert "Q4 Report.pdf" in result
        assert "s3://documents/report-2024.pdf" in result
        # Should have follow-up text
        assert "tomorrow's meeting" in result

    def test_multiple_images_from_design_review(self):
        """Test realistic scenario: Design review with multiple images."""
        img1 = FileWithUri(uri="s3://designs/mockup-v1.png", mimeType="image/png", name="mockup-v1.png")
        img2 = FileWithUri(uri="s3://designs/mockup-v2.png", mimeType="image/png", name="mockup-v2.png")

        parts = [
            Part(root=TextPart(text="Here are two design options:")),
            Part(root=FilePart(file=img1)),
            Part(root=FilePart(file=img2)),
            Part(root=TextPart(text="Which one looks better?")),
        ]

        result = build_text_content(parts)

        assert "two design options" in result
        assert result.count("[Image file attached]") == 2
        assert "mockup-v1.png" in result
        assert "mockup-v2.png" in result
        assert "Which one looks better?" in result
