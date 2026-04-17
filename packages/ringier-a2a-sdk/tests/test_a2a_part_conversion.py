"""Tests for a2a_parts_to_content shared utility."""

from a2a.types import DataPart, FilePart, FileWithBytes, FileWithUri, TextPart
from a2a.types import Part as A2APart

from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content


class TestTextOnly:
    """Tests for text_only=True (text extraction)."""

    def test_single_text_part(self):
        parts = [A2APart(root=TextPart(text="hello"))]
        assert a2a_parts_to_content(parts, text_only=True) == "hello"

    def test_multiple_text_parts_joined(self):
        parts = [A2APart(root=TextPart(text="a")), A2APart(root=TextPart(text="b"))]
        assert a2a_parts_to_content(parts, text_only=True) == "a\nb"

    def test_empty_parts_returns_empty_string(self):
        assert a2a_parts_to_content([], text_only=True) == ""

    def test_data_part_serialized_as_json(self):
        parts = [A2APart(root=DataPart(data={"key": "value"}))]
        assert a2a_parts_to_content(parts, text_only=True) == '{"key": "value"}'

    def test_mixed_text_and_data(self):
        parts = [
            A2APart(root=TextPart(text="Text content")),
            A2APart(root=DataPart(data={"key": "value"})),
        ]
        result = a2a_parts_to_content(parts, text_only=True)
        assert "Text content" in result
        assert '"key": "value"' in result

    def test_file_parts_are_ignored(self):
        parts = [
            A2APart(root=TextPart(text="hello")),
            A2APart(root=FilePart(file=FileWithUri(uri="https://example.com/img.png", mime_type="image/png"))),
        ]
        assert a2a_parts_to_content(parts, text_only=True) == "hello"

    def test_file_only_returns_empty(self):
        parts = [A2APart(root=FilePart(file=FileWithUri(uri="https://example.com/img.png", mime_type="image/png")))]
        assert a2a_parts_to_content(parts, text_only=True) == ""


class TestMultiModal:
    """Tests for text_only=False (full multi-modal conversion)."""

    def test_text_part_returns_list(self):
        parts = [A2APart(root=TextPart(text="hello"))]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0] == {"type": "text", "text": "hello"}

    def test_multiple_text_parts(self):
        parts = [A2APart(root=TextPart(text="a")), A2APart(root=TextPart(text="b"))]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0] == {"type": "text", "text": "a"}
        assert result[1] == {"type": "text", "text": "b"}

    def test_empty_parts_returns_empty_list(self):
        assert a2a_parts_to_content([]) == []

    def test_image_file_with_uri(self):
        parts = [
            A2APart(root=TextPart(text="see this")),
            A2APart(root=FilePart(file=FileWithUri(uri="https://img.example.com/cat.png", mime_type="image/png"))),
        ]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert result[0] == {"type": "text", "text": "see this"}
        assert result[1]["type"] == "image"
        assert result[1]["url"] == "https://img.example.com/cat.png"
        assert result[1]["mime_type"] == "image/png"

    def test_audio_file_with_uri(self):
        parts = [A2APart(root=FilePart(file=FileWithUri(uri="https://example.com/audio.mp3", mime_type="audio/mpeg")))]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert result[0]["type"] == "audio"
        assert result[0]["mime_type"] == "audio/mpeg"

    def test_video_file_with_uri(self):
        parts = [A2APart(root=FilePart(file=FileWithUri(uri="https://example.com/vid.mp4", mime_type="video/mp4")))]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert result[0]["type"] == "video"

    def test_generic_file_with_uri(self):
        parts = [
            A2APart(root=FilePart(file=FileWithUri(uri="https://example.com/doc.pdf", mime_type="application/pdf")))
        ]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert result[0]["type"] == "file"
        assert result[0]["mime_type"] == "application/pdf"

    def test_file_without_mime_type_uses_default(self):
        parts = [A2APart(root=FilePart(file=FileWithUri(uri="https://example.com/unknown")))]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert result[0]["type"] == "file"
        assert result[0]["mime_type"] == "application/octet-stream"

    def test_image_file_with_bytes(self):
        parts = [A2APart(root=FilePart(file=FileWithBytes(bytes="aW1hZ2VkYXRh", mime_type="image/jpeg")))]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert result[0]["type"] == "image"
        assert result[0]["base64"] == "aW1hZ2VkYXRh"

    def test_data_part_serialized_as_text_block(self):
        parts = [A2APart(root=DataPart(data={"key": "value"}))]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert result[0] == {"type": "text", "text": '{"key": "value"}'}

    def test_mixed_text_and_file(self):
        parts = [
            A2APart(root=TextPart(text="describe this")),
            A2APart(root=FilePart(file=FileWithUri(uri="https://example.com/img.jpg", mime_type="image/jpeg"))),
        ]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image"
