"""Tests for a2a_parts_to_content shared utility."""

from a2a.types import Part as A2APart
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from ringier_a2a_sdk.utils.a2a_part_conversion import a2a_parts_to_content


def _text(text: str) -> A2APart:
    return A2APart(text=text)


def _data(data: dict) -> A2APart:
    return A2APart(data=ParseDict(data, Value()))


def _file_url(url: str, media_type: str | None = None) -> A2APart:
    return A2APart(url=url, media_type=media_type) if media_type else A2APart(url=url)


def _file_bytes(raw: bytes, media_type: str) -> A2APart:
    return A2APart(raw=raw, media_type=media_type)


class TestTextOnly:
    """Tests for text_only=True (text extraction)."""

    def test_single_text_part(self):
        assert a2a_parts_to_content([_text("hello")], text_only=True) == "hello"

    def test_multiple_text_parts_joined(self):
        assert a2a_parts_to_content([_text("a"), _text("b")], text_only=True) == "a\nb"

    def test_empty_parts_returns_empty_string(self):
        assert a2a_parts_to_content([], text_only=True) == ""

    def test_data_part_serialized_as_json(self):
        assert a2a_parts_to_content([_data({"key": "value"})], text_only=True) == '{"key": "value"}'

    def test_mixed_text_and_data(self):
        result = a2a_parts_to_content([_text("Text content"), _data({"key": "value"})], text_only=True)
        assert "Text content" in result
        assert '"key": "value"' in result

    def test_file_parts_are_ignored(self):
        parts = [_text("hello"), _file_url("https://example.com/img.png", "image/png")]
        assert a2a_parts_to_content(parts, text_only=True) == "hello"

    def test_file_only_returns_empty(self):
        parts = [_file_url("https://example.com/img.png", "image/png")]
        assert a2a_parts_to_content(parts, text_only=True) == ""


class TestMultiModal:
    """Tests for text_only=False (full multi-modal conversion)."""

    def test_text_part_returns_list(self):
        result = a2a_parts_to_content([_text("hello")])
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0] == {"type": "text", "text": "hello"}

    def test_multiple_text_parts(self):
        result = a2a_parts_to_content([_text("a"), _text("b")])
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0] == {"type": "text", "text": "a"}
        assert result[1] == {"type": "text", "text": "b"}

    def test_empty_parts_returns_empty_list(self):
        assert a2a_parts_to_content([]) == []

    def test_image_file_with_uri(self):
        parts = [_text("see this"), _file_url("https://img.example.com/cat.png", "image/png")]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert result[0] == {"type": "text", "text": "see this"}
        assert result[1]["type"] == "image"
        assert result[1]["url"] == "https://img.example.com/cat.png"
        assert result[1]["mime_type"] == "image/png"

    def test_audio_file_with_uri(self):
        result = a2a_parts_to_content([_file_url("https://example.com/audio.mp3", "audio/mpeg")])
        assert isinstance(result, list)
        assert result[0]["type"] == "audio"
        assert result[0]["mime_type"] == "audio/mpeg"

    def test_video_file_with_uri(self):
        result = a2a_parts_to_content([_file_url("https://example.com/vid.mp4", "video/mp4")])
        assert isinstance(result, list)
        assert result[0]["type"] == "video"

    def test_generic_file_with_uri(self):
        result = a2a_parts_to_content([_file_url("https://example.com/doc.pdf", "application/pdf")])
        assert isinstance(result, list)
        assert result[0]["type"] == "file"
        assert result[0]["mime_type"] == "application/pdf"

    def test_file_without_mime_type_uses_default(self):
        result = a2a_parts_to_content([_file_url("https://example.com/unknown")])
        assert isinstance(result, list)
        assert result[0]["type"] == "file"
        assert result[0]["mime_type"] == "application/octet-stream"

    def test_image_file_with_bytes(self):
        # ``raw`` carries the file bytes directly; the converter base64-encodes them.
        # b"imagedata" -> base64 "aW1hZ2VkYXRh"
        result = a2a_parts_to_content([_file_bytes(b"imagedata", "image/jpeg")])
        assert isinstance(result, list)
        assert result[0]["type"] == "image"
        assert result[0]["base64"] == "aW1hZ2VkYXRh"

    def test_data_part_returns_non_standard_block(self):
        result = a2a_parts_to_content([_data({"key": "value"})])
        assert isinstance(result, list)
        assert result[0] == {
            "type": "non_standard",
            "value": {"media_type": "application/json", "data": {"key": "value"}},
        }
    # TODO: currently it always becomes application/json, noted in a2a_parts_to_content

    def test_mixed_text_and_file(self):
        parts = [_text("describe this"), _file_url("https://example.com/img.jpg", "image/jpeg")]
        result = a2a_parts_to_content(parts)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image"
