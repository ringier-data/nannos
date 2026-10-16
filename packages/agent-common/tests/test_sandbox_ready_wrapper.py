"""Tests for ReadySandboxWrapper — sandbox not-ready detection."""

import pytest

from agent_common.core.sandbox_ready_wrapper import ReadySandboxWrapper, SandboxNotReadyError


class _FakeExecuteResponse:
    def __init__(self, output: str, exit_code: int = 0):
        self.output = output
        self.exit_code = exit_code
        self.truncated = False


class _FakeSandbox:
    """Minimal fake sandbox for testing the wrapper."""

    def __init__(self, execute_output: str = '{"content": "hello"}'):
        self._execute_output = execute_output
        self._id = "fake-sandbox-123"
        self.execute_count = 0

    @property
    def id(self) -> str:
        return self._id

    def execute(self, command: str, *, timeout: int | None = None):
        self.execute_count += 1
        return _FakeExecuteResponse(self._execute_output)

    async def aexecute(self, command: str, *, timeout: int | None = None):
        self.execute_count += 1
        return _FakeExecuteResponse(self._execute_output)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list:
        return []

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list:
        return []

    def some_other_method(self) -> str:
        return "delegated"


class TestReadySandboxWrapper:
    def test_passes_through_normal_execute(self):
        sandbox = _FakeSandbox('{"content":"test"}')
        wrapper = ReadySandboxWrapper(sandbox)

        result = wrapper.execute("cat /file.txt")
        assert result.output == '{"content":"test"}'
        assert sandbox.execute_count == 1

    @pytest.mark.asyncio
    async def test_passes_through_normal_aexecute(self):
        sandbox = _FakeSandbox('{"content":"test"}')
        wrapper = ReadySandboxWrapper(sandbox)

        result = await wrapper.aexecute("cat /file.txt")
        assert result.output == '{"content":"test"}'
        assert sandbox.execute_count == 1

    def test_raises_on_sandbox_not_ready(self):
        sandbox = _FakeSandbox("Sandbox is not ready")
        wrapper = ReadySandboxWrapper(sandbox)

        with pytest.raises(SandboxNotReadyError, match="Sandbox is not ready"):
            wrapper.execute("cat /file.txt")

    @pytest.mark.asyncio
    async def test_raises_on_sandbox_not_ready_async(self):
        sandbox = _FakeSandbox("Sandbox is not ready")
        wrapper = ReadySandboxWrapper(sandbox)

        with pytest.raises(SandboxNotReadyError, match="Sandbox is not ready"):
            await wrapper.aexecute("cat /file.txt")

    def test_raises_on_container_not_running(self):
        sandbox = _FakeSandbox("Error: container is not running")
        wrapper = ReadySandboxWrapper(sandbox)

        with pytest.raises(SandboxNotReadyError):
            wrapper.execute("ls /")

    def test_raises_on_container_starting(self):
        sandbox = _FakeSandbox("Please wait, container is starting up")
        wrapper = ReadySandboxWrapper(sandbox)

        with pytest.raises(SandboxNotReadyError):
            wrapper.execute("ls /")

    def test_delegates_other_attributes(self):
        sandbox = _FakeSandbox()
        wrapper = ReadySandboxWrapper(sandbox)

        assert wrapper.some_other_method() == "delegated"

    def test_id_property(self):
        sandbox = _FakeSandbox()
        wrapper = ReadySandboxWrapper(sandbox)

        assert wrapper.id == "fake-sandbox-123"

    def test_upload_files_passes_through(self):
        sandbox = _FakeSandbox()
        wrapper = ReadySandboxWrapper(sandbox)

        result = wrapper.upload_files([("/test.txt", b"hello")])
        assert result == []

    @pytest.mark.asyncio
    async def test_aupload_files_passes_through(self):
        sandbox = _FakeSandbox()
        wrapper = ReadySandboxWrapper(sandbox)

        result = await wrapper.aupload_files([("/test.txt", b"hello")])
        assert result == []

    def test_upload_raises_on_not_ready_runtime_error(self):
        """upload_files that raises RuntimeError with not-ready message should become SandboxNotReadyError."""

        class _FailingSandbox(_FakeSandbox):
            def upload_files(self, files):
                raise RuntimeError("Sandbox is not ready")

        sandbox = _FailingSandbox()
        wrapper = ReadySandboxWrapper(sandbox)

        with pytest.raises(SandboxNotReadyError):
            wrapper.upload_files([("/test.txt", b"hello")])

    @pytest.mark.asyncio
    async def test_aupload_raises_on_not_ready_runtime_error(self):
        """aupload_files that raises RuntimeError with not-ready message should become SandboxNotReadyError."""

        class _FailingSandbox(_FakeSandbox):
            async def aupload_files(self, files):
                raise RuntimeError("container is not running")

        sandbox = _FailingSandbox()
        wrapper = ReadySandboxWrapper(sandbox)

        with pytest.raises(SandboxNotReadyError):
            await wrapper.aupload_files([("/test.txt", b"hello")])

    def test_does_not_raise_on_empty_output(self):
        """Empty output should not trigger not-ready detection."""
        sandbox = _FakeSandbox("")
        wrapper = ReadySandboxWrapper(sandbox)

        result = wrapper.execute("true")
        assert result.output == ""

    def test_case_insensitive_detection(self):
        """Detection should be case-insensitive."""
        sandbox = _FakeSandbox("SANDBOX IS NOT READY")
        wrapper = ReadySandboxWrapper(sandbox)

        with pytest.raises(SandboxNotReadyError):
            wrapper.execute("cat /file.txt")

    def test_isinstance_sandbox_backend_protocol(self):
        """Wrapper must pass isinstance checks for SandboxBackendProtocol."""
        from deepagents.backends.protocol import SandboxBackendProtocol

        sandbox = _FakeSandbox()
        wrapper = ReadySandboxWrapper(sandbox)
        assert isinstance(wrapper, SandboxBackendProtocol)


class _SearchSandbox(_FakeSandbox):
    """Search methods that run their command on the inner backend, as BaseSandbox's do."""

    def __init__(self, error: str | None):
        super().__init__()
        self._error = error
        self.grep_kwargs: dict = {}

    def _result(self):
        from deepagents.backends.protocol import GlobResult

        return GlobResult(error=self._error) if self._error else GlobResult(matches=[])

    def glob(self, pattern, path=None):
        return self._result()

    async def aglob(self, pattern, path=None):
        return self._result()

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        self.grep_kwargs = {"max_count": max_count}
        return self._result()

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        self.grep_kwargs = {"max_count": max_count}
        return self._result()


class TestSearchNotReady:
    """BaseSandbox glob/grep bypass the wrapper's execute(); a not-ready sandbox must still
    raise the retryable error instead of reaching the model as a search error."""

    @pytest.mark.asyncio
    async def test_a_not_ready_glob_raises(self):
        wrapper = ReadySandboxWrapper(_SearchSandbox("glob helper emitted unexpected output: Sandbox is not ready"))

        with pytest.raises(SandboxNotReadyError):
            await wrapper.aglob("*.py", "/home")

    @pytest.mark.asyncio
    async def test_a_not_ready_grep_raises(self):
        wrapper = ReadySandboxWrapper(_SearchSandbox("container is starting"))

        with pytest.raises(SandboxNotReadyError):
            await wrapper.agrep("TODO", "/home")

    def test_an_ordinary_search_error_is_returned(self):
        wrapper = ReadySandboxWrapper(_SearchSandbox("Invalid glob pattern '..': refused"))

        assert wrapper.glob("..").error == "Invalid glob pattern '..': refused"

    def test_max_count_reaches_the_sandbox(self):
        from deepagents.backends.protocol import _method_accepts_max_count

        sandbox = _SearchSandbox(None)
        wrapper = ReadySandboxWrapper(sandbox)

        assert _method_accepts_max_count(ReadySandboxWrapper, "grep")
        wrapper.grep("TODO", "/home", max_count=50)
        assert sandbox.grep_kwargs == {"max_count": 50}


def test_a_sandbox_still_starting_is_retried():
    """The wrapper raises it so the tool-retry middleware backs off and tries again."""
    from agent_common.core.graph_utils import _should_retry_tool_error

    assert _should_retry_tool_error(SandboxNotReadyError("Sandbox is not ready")) is True
    assert _should_retry_tool_error(RuntimeError("boom")) is False
