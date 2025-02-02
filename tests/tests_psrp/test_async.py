import asyncio
import base64
import datetime
import logging
import typing as t
import uuid
import xml.etree.ElementTree as ElementTree

import psrpcore
import pytest
import pytest_mock

import psrp
import psrp._connection.wsman
import psrp._winrs
import psrp._wsman
from psrp._compat import asyncio_create_task
from psrp._connection.out_of_proc import ps_data_packet, ps_guid_packet


class PSEventCallbacks:
    def __init__(self) -> None:
        self.events: t.List[psrpcore.PSRPEvent] = []

    async def __call__(self, event: psrpcore.PSRPEvent) -> None:
        self.events.append(event)


class PSDataCallbacks:
    def __init__(self) -> None:
        self.data: t.List[t.Any] = []

    async def __call__(self, data: t.Any) -> None:
        self.data.append(data)


class BadOutOfProcTransport(psrp.AsyncOutOfProcConnection):
    async def read(self) -> t.Optional[bytes]:
        return b"Raw error message from target\n"

    async def write(
        self,
        data: bytes,
    ) -> None:
        pass


class CustomOutOfProcInfo(psrp.ConnectionInfo):
    def __init__(
        self,
        incoming: "asyncio.Queue[bytes]",
        outgoing: "asyncio.Queue[bytes]",
    ) -> None:
        self._incoming = incoming
        self._outgoing = outgoing

    async def create_async(
        self,
        pool: psrpcore.ClientRunspacePool,
        callback: psrp.AsyncEventCallable,
    ) -> psrp.AsyncConnection:
        return CustomOutOfProcTransport(pool, callback, self._incoming, self._outgoing)


class CustomOutOfProcTransport(psrp.AsyncOutOfProcConnection):
    def __init__(
        self,
        pool: psrpcore.ClientRunspacePool,
        callback: psrp.AsyncEventCallable,
        incoming: "asyncio.Queue[bytes]",
        outgoing: "asyncio.Queue[bytes]",
    ) -> None:
        super().__init__(pool, callback)
        self._incoming = incoming
        self._outgoing = outgoing

    async def read(self) -> t.Optional[bytes]:
        return await self._incoming.get()

    async def write(
        self,
        data: bytes,
    ) -> None:
        await self._outgoing.put(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_open_runspace(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened
        assert rp.max_runspaces == 1
        assert rp.min_runspaces == 1
        assert rp.pipeline_table == {}
        assert isinstance(rp.application_private_data, dict)
        assert isinstance(rp.max_payload_size, int)

    assert rp.state == psrpcore.types.RunspacePoolState.Closed


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_open_runspace_large_app_args(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    app_args: t.Dict[str, t.Any] = {"key": "a" * 1_048_576}
    async with psrp.AsyncRunspacePool(connection, application_arguments=app_args) as rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened
        assert rp.max_runspaces == 1
        assert rp.min_runspaces == 1
        assert rp.pipeline_table == {}
        assert isinstance(rp.application_private_data, dict)

    assert rp.state == psrpcore.types.RunspacePoolState.Closed


@pytest.mark.asyncio
async def test_open_runspace_with_failure() -> None:
    incoming: "asyncio.Queue[bytes]" = asyncio.Queue()
    outgoing: "asyncio.Queue[bytes]" = asyncio.Queue()
    conn = CustomOutOfProcInfo(incoming, outgoing)

    async def put_on_recv() -> None:
        await outgoing.get()
        await incoming.put(b"Raw error message from target\n")

    task = asyncio_create_task(put_on_recv())

    rp = psrp.AsyncRunspacePool(conn)

    expected = "Failed to parse response: Raw error message from target"
    with pytest.raises(psrp.PSRPError, match=expected):
        async with rp:
            pass

    await task

    with pytest.raises(psrp.PSRPError, match=expected):
        await rp.open()


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_open_runspace_min_max(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection, min_runspaces=2, max_runspaces=3) as rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened
        assert rp.max_runspaces == 3
        assert rp.min_runspaces == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_open_runspace_invalid_min_max(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")

    with pytest.raises(
        ValueError, match="min_runspaces must be greater than 0 and max_runspaces must be greater than min_runspaces"
    ):
        async with psrp.AsyncRunspacePool(connection, min_runspaces=2, max_runspaces=1) as rp:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_runspace_set_min_max(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        assert rp.min_runspaces == 1
        assert rp.max_runspaces == 1

        actual = await rp.get_available_runspaces()
        assert actual == 1

        # Will fail as max is lower than 1
        actual = await rp.set_min_runspaces(2)
        assert actual is False
        assert rp.min_runspaces == 1

        actual = await rp.set_max_runspaces(2)
        assert actual
        assert rp.max_runspaces == 2

        actual = await rp.set_min_runspaces(2)
        assert actual
        assert rp.min_runspaces == 2

        actual = await rp.set_min_runspaces(-1)
        assert actual is False
        assert rp.min_runspaces == 2

        actual = await rp.get_available_runspaces()
        assert actual == 2

        # Test setting same values does nothing
        actual = await rp.set_min_runspaces(2)
        assert actual

        actual = await rp.set_max_runspaces(2)
        assert actual


@pytest.mark.asyncio
async def test_runspace_disconnect(psrp_wsman: psrp.ConnectionInfo) -> None:
    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened
        await rp.disconnect()
        assert rp.state == psrpcore.types.RunspacePoolState.Disconnected

    assert rp.state == psrpcore.types.RunspacePoolState.Disconnected

    # Reconnect back as the same client
    async with rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened
        await rp.reset_runspace_state()
        await rp.disconnect()

    assert rp.state == psrpcore.types.RunspacePoolState.Disconnected

    # Connect back as a new client
    async for rp in psrp.AsyncRunspacePool.get_runspace_pools(psrp_wsman):
        assert rp.state == psrpcore.types.RunspacePoolState.Disconnected

        async with rp:
            assert rp.state == psrpcore.types.RunspacePoolState.Opened
            await rp.reset_runspace_state()

        assert rp.state == psrpcore.types.RunspacePoolState.Closed


@pytest.mark.asyncio
async def test_runspace_disconnect_without_timeout_and_buffer_mode(psrp_wsman: psrp.WSManInfo) -> None:
    psrp_wsman.buffer_mode = psrp.OutputBufferingMode.DROP
    psrp_wsman.idle_timeout = 10

    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened
        await rp.disconnect()
        assert rp.state == psrpcore.types.RunspacePoolState.Disconnected

    assert rp.state == psrpcore.types.RunspacePoolState.Disconnected

    async with rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened
        await rp.reset_runspace_state()

    assert rp.state == psrpcore.types.RunspacePoolState.Closed


@pytest.mark.asyncio
async def test_runspace_not_available(psrp_wsman: psrp.ConnectionInfo) -> None:
    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        async for runspace in psrp.AsyncRunspacePool.get_runspace_pools(psrp_wsman):
            if runspace._pool.runspace_pool_id != rp._pool.runspace_pool_id:
                continue

            assert not runspace.is_available
            assert runspace.state == psrpcore.types.RunspacePoolState.Opened

            expected = "This Runspace Pool is connected to another client"
            with pytest.raises(psrp.RunspaceNotAvailable, match=expected):
                async with runspace:
                    pass

            with pytest.raises(psrp.RunspaceNotAvailable, match=expected):
                await runspace.open()

            with pytest.raises(psrp.RunspaceNotAvailable, match=expected):
                await runspace.connect()

            with pytest.raises(psrp.RunspaceNotAvailable, match=expected):
                await runspace.reset_runspace_state()


@pytest.mark.asyncio
async def test_runspace_get_pools_ignore_other_resources(
    psrp_wsman: psrp.WSManInfo,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_receive_enumeration = mocker.MagicMock(
        return_value=(
            [
                psrp._winrs.WinRS(
                    psrp._wsman.WSMan("uri"),
                    "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/cmd",
                )
            ],
            [],
        )
    )
    monkeypatch.setattr(psrp._connection.wsman, "receive_winrs_enumeration", mock_receive_enumeration)
    actual = [rp async for rp in psrp.AsyncRunspacePool.get_runspace_pools(psrp_wsman)]
    assert actual == []


@pytest.mark.asyncio
async def test_runspace_disconnect_unsupported(psrp_proc: psrp.ConnectionInfo) -> None:
    async with psrp.AsyncRunspacePool(psrp_proc) as rp:
        with pytest.raises(
            NotImplementedError, match="Disconnection operation not implemented on this connection type"
        ):
            await rp.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_runspace_application_arguments(conn: str, request: pytest.FixtureRequest) -> None:
    app_args = {
        "test_var": "abcdef12345",
        "bool": True,
    }
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection, application_arguments=app_args) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("$PSSenderInfo.ApplicationArguments")

        actual = await ps.invoke()
        assert len(actual) == 1
        assert isinstance(actual[0], dict)
        assert actual[0] == app_args


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_runspace_reset_state(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("$global:TestVar = 'foo'")
        await ps.invoke()

        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("$global:TestVar")
        actual = await ps.invoke()
        assert actual == ["foo"]

        actual_res = await rp.reset_runspace_state()
        assert actual_res

        actual = await ps.invoke()
        assert actual == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_runspace_host_call(
    conn: str,
    request: pytest.FixtureRequest,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_line_event = asyncio.Event()

    rp_host = psrp.PSHost(ui=psrp.PSHostUI())
    rp_write_line = mocker.MagicMock()

    def write_line(line: str) -> None:
        write_line_event.set()
        rp_write_line(line)

    monkeypatch.setattr(rp_host.ui, "read_line", lambda: "runspace line")
    monkeypatch.setattr(rp_host.ui, "write_line", write_line)

    ps_host = psrp.PSHost(ui=psrp.PSHostUI())
    ps_write_line = mocker.MagicMock()
    monkeypatch.setattr(ps_host.ui, "read_line", lambda: "pipeline line")
    monkeypatch.setattr(ps_host.ui, "write_line", ps_write_line)

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection, host=rp_host) as rp:
        ps = psrp.AsyncPowerShell(rp, host=ps_host)
        ps.add_script(
            """
            $rs = [Runspace]::DefaultRunspace
            $rsHost = $rs.GetType().GetProperty("Host", 60).GetValue($rs)
            $rsHost.UI.WriteLine("host output")
            $rsHost.UI.ReadLine()
            """
        )
        task = await ps.invoke_async()
        await write_line_event.wait()

        actual = await task
        assert actual == ["runspace line"]
        rp_write_line.assert_called_once_with("host output")
        ps_write_line.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_runspace_host_call_failure(
    conn: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_line_event = asyncio.Event()

    rp_host = psrp.PSHost(ui=psrp.PSHostUI())

    def write_line(line: str) -> None:
        write_line_event.set()
        raise NotImplementedError()

    monkeypatch.setattr(rp_host.ui, "read_line", lambda: "runspace line")
    monkeypatch.setattr(rp_host.ui, "write_line", write_line)

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection, host=rp_host) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script(
            """
            $rs = [Runspace]::DefaultRunspace
            $rsHost = $rs.GetType().GetProperty("Host", 60).GetValue($rs)
            $rsHost.UI.WriteLine("host output")
            $rsHost.UI.ReadLine()
            """
        )
        task = await ps.invoke_async()
        await write_line_event.wait()

        actual = await task
        assert actual == ["runspace line"]
        assert ps.streams.error == []
        assert len(rp.streams.error) == 1
        assert isinstance(rp.streams.error[0], psrpcore.types.ErrorRecord)
        assert str(rp.streams.error[0]) == "NotImplementedError when running HostMethodIdentifier.WriteLine2"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_runspace_user_event(conn: str, request: pytest.FixtureRequest) -> None:
    received_callback = asyncio.Event()

    async def user_event_callback(event: psrpcore.UserEventEvent) -> None:
        received_callback.set()

    callback = PSEventCallbacks()
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        rp.user_event += callback
        rp.user_event += user_event_callback

        async def input_gen() -> t.AsyncIterator[int]:
            await received_callback.wait()
            yield 1

        ps = psrp.AsyncPowerShell(rp)
        ps.state_changed += callback
        ps.add_script(
            """
            $null = $Host.Runspace.Events.SubscribeEvent(
                $null,
                "EventIdentifier",
                "EventIdentifier",
                $null,
                $null,
                $true,
                $true)
            $null = $Host.Runspace.Events.GenerateEvent(
                "EventIdentifier",
                "sender",
                @("my", "args"),
                "extra data")
            $input
            """
        )
        await ps.invoke(input_data=input_gen())

        assert len(callback.events) == 2
        assert isinstance(callback.events[0], psrpcore.UserEventEvent)
        assert callback.events[0].event.EventIdentifier == 1
        assert callback.events[0].event.ComputerName is None
        assert callback.events[0].event.MessageData == "extra data"
        assert callback.events[0].event.Sender == "sender"
        assert callback.events[0].event.SourceArgs == ["my", "args"]
        assert callback.events[0].event.SourceIdentifier == "EventIdentifier"
        assert isinstance(callback.events[0].event.TimeGenerated, datetime.datetime)
        assert isinstance(callback.events[1], psrpcore.PipelineStateEvent)

        # Validate that it can remove the event and a user event is just lost in the ether
        rp.user_event -= callback
        ps.state_changed -= callback

        await ps.invoke()
        assert len(callback.events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_runspace_powershell_event_exception(
    conn: str,
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="psrp._async")

    async def failure_callback(event: psrpcore.PSRPEvent) -> None:
        raise Exception("unknown failure")

    connection = request.getfixturevalue(f"psrp_{conn}")
    rp = psrp.AsyncRunspacePool(connection)
    rp.user_event += failure_callback
    rp.state_changed += failure_callback

    async with rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.state_changed += failure_callback

        ps.add_script(
            """
            $null = $Host.Runspace.Events.SubscribeEvent(
                $null,
                "EventIdentifier",
                "EventIdentifier",
                $null,
                $null,
                $true,
                $true)
            $null = $Host.Runspace.Events.GenerateEvent(
                "EventIdentifier",
                "sender",
                @("my", "args"),
                "extra data")
            # Ensure the event comes before the script ends
            Start-Sleep -Milliseconds 500
            """
        )
        await ps.invoke()

        assert len(caplog.records) == 3

        assert caplog.records[0].levelname == "ERROR"
        assert caplog.records[0].message == "Failed to invoke callback for RunspacePool state_changed"
        assert isinstance(caplog.records[0].exc_info, tuple)
        assert isinstance(caplog.records[0].exc_info[1], Exception)
        assert str(caplog.records[0].exc_info[1]) == "unknown failure"

        assert caplog.records[1].levelname == "ERROR"
        assert caplog.records[1].message == "Failed to invoke callback for RunspacePool user_event"
        assert isinstance(caplog.records[1].exc_info, tuple)
        assert isinstance(caplog.records[1].exc_info[1], Exception)
        assert str(caplog.records[1].exc_info[1]) == "unknown failure"

        assert caplog.records[2].levelname == "ERROR"
        assert caplog.records[2].message == "Failed to invoke callback for Pipeline state_changed"
        assert isinstance(caplog.records[2].exc_info, tuple)
        assert isinstance(caplog.records[2].exc_info[1], Exception)
        assert str(caplog.records[2].exc_info[1]) == "unknown failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_runspace_stream_data(conn: str, request: pytest.FixtureRequest) -> None:
    # This is not a scenario that is valid in a normal pwsh endpoint but I've seen it before with custom PSRemoting
    # endpoints (Exchange Online).
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        server = psrpcore.ServerRunspacePool()
        server.runspace_pool_id = rp._pool.runspace_pool_id
        server.prepare_message(psrpcore.types.DebugRecordMsg(Message="debug"))
        server.prepare_message(
            psrpcore.types.ErrorRecordMsg(
                Exception=psrpcore.types.NETException(Message="error"),
                CategoryInfo=psrpcore.types.ErrorCategoryInfo(),
            )
        )
        server.prepare_message(psrpcore.types.InformationRecordMsg(MessageData="information"))
        server.prepare_message(psrpcore.types.ProgressRecordMsg(Activity="progress"))
        server.prepare_message(psrpcore.types.VerboseRecordMsg(Message="verbose"))
        server.prepare_message(psrpcore.types.WarningRecordMsg(Message="warning"))
        while True:
            msg = server.data_to_send()
            if not msg:
                break
            assert rp._connection is not None
            await rp._connection.process_response(msg)

        assert len(rp.streams.debug) == 1
        assert isinstance(rp.streams.debug[0], psrpcore.types.DebugRecord)
        assert rp.streams.debug[0].Message == "debug"

        assert len(rp.streams.error) == 1
        assert isinstance(rp.streams.error[0], psrpcore.types.ErrorRecord)
        assert str(rp.streams.error[0]) == "error"

        assert len(rp.streams.information) == 1
        assert isinstance(rp.streams.information[0], psrpcore.types.InformationRecord)
        assert rp.streams.information[0].MessageData == "information"

        assert len(rp.streams.progress) == 1
        assert isinstance(rp.streams.progress[0], psrpcore.types.ProgressRecord)
        assert rp.streams.progress[0].Activity == "progress"

        assert len(rp.streams.verbose) == 1
        assert isinstance(rp.streams.verbose[0], psrpcore.types.VerboseRecord)
        assert rp.streams.verbose[0].Message == "verbose"

        assert len(rp.streams.warning) == 1
        assert isinstance(rp.streams.warning[0], psrpcore.types.WarningRecord)
        assert rp.streams.warning[0].Message == "warning"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_run_powershell(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("echo 'hi'")
        actual = await ps.invoke()
        assert actual == ["hi"]


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_run_powershell_close_before_complete(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        out = psrp.AsyncPSDataCollection[t.Any]()
        out_received = asyncio.Event()

        async def wait_out(event: psrpcore.PSRPEvent) -> None:
            out_received.set()

        out.data_added += wait_out

        ps.add_script("1; Start-Sleep -Seconds 60")
        task = await ps.invoke_async(output_stream=out)
        await out_received.wait()

    with pytest.raises(psrp.PipelineStopped, match="The pipeline has been stopped."):
        await task
    assert ps.state == psrpcore.types.PSInvocationState.Stopped
    await ps.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_create_disconnected_power_shells_fail_state(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        expected = (
            "Can only enumerate disconnected PowerShell pipelines on a Runspace Pool retrieved with get_runspace_pools"
        )
        with pytest.raises(psrp.PSRPError, match=expected):
            rp.create_disconnected_power_shells()


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_secure_string(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        secure_string = psrpcore.types.PSSecureString("my secret")
        ps.add_command("Write-Output").add_parameter("InputObject", secure_string)
        actual = await ps.invoke()
        assert len(actual) == 1
        assert isinstance(actual[0], psrpcore.types.PSSecureString)
        assert actual[0].decrypt() == "my secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_receive_secure_string(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        ps.add_command("ConvertTo-SecureString").add_parameters(AsPlainText=True, Force=True, String="secret")
        actual = await ps.invoke()
        assert len(actual) == 1
        assert isinstance(actual[0], psrpcore.types.PSSecureString)

        with pytest.raises(
            psrpcore.MissingCipherError,
            match=r"Cannot \(de\)serialize a secure string without an exchanged session key",
        ):
            actual[0].decrypt()

        await rp.exchange_key()
        assert actual[0].decrypt() == "secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["named_pipe", "proc", "ssh", "win_ps_ssh", "wsman"])
async def test_powershell_streams(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        ps.add_script(
            """
            $DebugPreference = 'Continue'
            $VerbosePreference = 'Continue'
            $WarningPreference = 'Continue'

            Write-Debug -Message debug
            Write-Error -Message error
            Write-Information -MessageData information
            Write-Output -InputObject output
            Write-Progress -Activity progress -Status done -PercentComplete 100
            Write-Verbose -Message verbose
            Write-Warning -Message warning
            """
        )

        for idx in range(2):
            actual = await ps.invoke()

            assert ps.had_errors  # An error record sets this
            assert actual == ["output"]

            assert len(ps.streams.debug) == 1
            assert ps.streams.debug[0].Message == "debug"

            assert len(ps.streams.error) == 1
            assert ps.streams.error[0].Exception.Message == "error"

            assert len(ps.streams.information) == 1
            assert ps.streams.information[0].MessageData == "information"

            # WSMan always adds another progress record, remove to align the tests
            if idx == 0 and isinstance(connection, (psrp.WSManInfo, psrp.WinPSSSHInfo)):
                ps.streams.progress.pop(0)
            assert len(ps.streams.progress) == 1
            assert ps.streams.progress[0].Activity == "progress"
            assert ps.streams.progress[0].PercentComplete == 100
            assert ps.streams.progress[0].StatusDescription == "done"

            assert len(ps.streams.verbose) == 1
            assert ps.streams.verbose[0].Message == "verbose"

            assert len(ps.streams.warning) == 1
            assert ps.streams.warning[0].Message == "warning"

            ps.streams.clear_streams()
            assert len(ps.streams.debug) == 0
            assert len(ps.streams.error) == 0
            assert len(ps.streams.information) == 0
            assert len(ps.streams.progress) == 0
            assert len(ps.streams.verbose) == 0
            assert len(ps.streams.warning) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_invalid_command(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_command("Fake-Command")

        with pytest.raises(psrp.PipelineFailed, match="The term 'Fake-Command' is not recognized"):
            await ps.invoke()

        # On an exception for Invoke() pwsh does not set this so it's also not set here.
        assert not ps.had_errors


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_state_changed(conn: str, request: pytest.FixtureRequest) -> None:
    callbacks = PSEventCallbacks()

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.state_changed += callbacks

        ps.add_script('echo "hi"')
        await ps.invoke()
        assert len(callbacks.events) == 1
        assert isinstance(callbacks.events[0], psrpcore.PipelineStateEvent)
        assert callbacks.events[0].state == ps.state

        ps.state_changed -= callbacks

        await ps.invoke()
        assert len(callbacks.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_stream_events(conn: str, request: pytest.FixtureRequest) -> None:
    callbacks = PSDataCallbacks()
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script('$VerbosePreference = "Continue"; Write-Verbose -Message verbose')

        ps.streams.verbose.data_adding += callbacks
        ps.streams.verbose.data_added += callbacks
        ps.streams.verbose.on_completed += callbacks
        ps.state_changed += callbacks

        await ps.invoke()

        assert len(callbacks.data) == 3
        assert isinstance(callbacks.data[0], psrpcore.types.VerboseRecord)
        assert callbacks.data[0].Message == "verbose"
        assert isinstance(callbacks.data[1], psrpcore.types.VerboseRecord)
        assert callbacks.data[1].Message == "verbose"
        assert isinstance(callbacks.data[2], psrpcore.PipelineStateEvent)
        assert len(ps.streams.verbose) == 1

        await ps.streams.verbose.complete()
        assert len(callbacks.data) == 4
        assert isinstance(callbacks.data[3], bool)
        assert callbacks.data[3] is True

        with pytest.raises(ValueError, match="Objects cannot be added to a closed buffer"):
            ps.streams.verbose.append(ps.streams.verbose[0])

        with pytest.raises(ValueError, match="Objects cannot be added to a closed buffer"):
            ps.streams.verbose.insert(0, ps.streams.verbose[0])

        await ps.invoke()
        assert len(callbacks.data) == 5
        assert isinstance(callbacks.data[4], psrpcore.PipelineStateEvent)
        assert len(ps.streams.verbose) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_stream_events_exception(
    conn: str,
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="psrp._async")

    async def failure_callback(value: t.Any) -> None:
        raise Exception("unknown failure")

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script('$VerbosePreference = "Continue"; Write-Verbose -Message verbose')
        ps.streams.verbose.data_adding += failure_callback
        ps.streams.verbose.data_added += failure_callback
        ps.streams.verbose.on_completed += failure_callback

        await ps.invoke()

        assert len(caplog.records) == 2

        assert caplog.records[0].levelname == "ERROR"
        assert caplog.records[0].message == "Failed to invoke callback for PSDataCollection data_adding"
        assert isinstance(caplog.records[0].exc_info, tuple)
        assert isinstance(caplog.records[0].exc_info[1], Exception)
        assert str(caplog.records[0].exc_info[1]) == "unknown failure"

        assert caplog.records[1].levelname == "ERROR"
        assert caplog.records[1].message == "Failed to invoke callback for PSDataCollection data_added"
        assert isinstance(caplog.records[1].exc_info, tuple)
        assert isinstance(caplog.records[1].exc_info[1], Exception)
        assert str(caplog.records[1].exc_info[1]) == "unknown failure"

        await ps.streams.verbose.complete()

        assert len(caplog.records) == 3

        assert caplog.records[2].levelname == "ERROR"
        assert caplog.records[2].message == "Failed to invoke callback for PSDataCollection on_completed"
        assert isinstance(caplog.records[2].exc_info, tuple)
        assert isinstance(caplog.records[2].exc_info[1], Exception)
        assert str(caplog.records[2].exc_info[1]) == "unknown failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_blocking_iterator(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        out = psrp.AsyncPSDataCollection[t.Any](blocking_iterator=True)
        out.append("manual 1")
        out.insert(0, "manual 0")

        async def state_callback(event: psrpcore.PipelineStateEvent) -> None:
            await out.complete()

        ps.state_changed += state_callback

        ps.add_script("1, 2, 3, 4, 5")
        task = await ps.invoke_async(output_stream=out)

        result = []
        async for data in out:
            result.append(data)

        assert ps.state == psrpcore.types.PSInvocationState.Completed
        assert result == ["manual 0", "manual 1", 1, 2, 3, 4, 5]

        task_out = await task
        assert task_out == []


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_host_call(
    conn: str,
    request: pytest.FixtureRequest,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rp_host = psrp.PSHost(ui=psrp.PSHostUI())
    rp_write_line = mocker.MagicMock()
    monkeypatch.setattr(rp_host.ui, "read_line", lambda: "runspace line")
    monkeypatch.setattr(rp_host.ui, "write_line", rp_write_line)

    ps_host = psrp.PSHost(ui=psrp.PSHostUI())
    ps_write_line = mocker.MagicMock()
    monkeypatch.setattr(ps_host.ui, "read_line", lambda: "pipeline line")
    monkeypatch.setattr(ps_host.ui, "write_line", ps_write_line)

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection, host=rp_host) as rp:
        ps = psrp.AsyncPowerShell(rp, host=ps_host)
        ps.add_script(
            """
            $Host.UI.ReadLine()
            $Host.UI.WriteLine("host output")
            """
        )
        actual = await ps.invoke()
        assert actual == ["pipeline line"]
        rp_write_line.assert_not_called()
        ps_write_line.assert_called_once_with("host output")


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_host_call_failure(conn: str, request: pytest.FixtureRequest) -> None:
    ps_host = psrp.PSHost(ui=psrp.PSHostUI())

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp, host=ps_host)
        ps.add_script(
            """
            $Host.UI.WriteLine("host output")
            $Host.UI.ReadLine()
            """
        )
        actual = await ps.invoke()
        assert actual == []
        assert len(rp.streams.error) == 0
        assert len(ps.streams.error) == 2
        assert isinstance(ps.streams.error[0], psrpcore.types.ErrorRecord)
        assert str(ps.streams.error[0]) == "NotImplementedError when running HostMethodIdentifier.WriteLine2"
        assert str(ps.streams.error[1]) == (
            'Exception calling "ReadLine" with "0" argument(s): "NotImplementedError when running '
            'HostMethodIdentifier.ReadLine"'
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_host_call_with_secure_string(
    conn: str,
    request: pytest.FixtureRequest,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_line_as_secure_string = mocker.MagicMock()
    read_line_as_secure_string.return_value = psrpcore.types.PSSecureString("secret")
    host = psrp.PSHost(ui=psrp.PSHostUI())
    monkeypatch.setattr(host.ui, "read_line_as_secure_string", read_line_as_secure_string)

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection, host=host) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("$host.UI.ReadLineAsSecureString()")

        actual = await ps.invoke()
        assert len(ps.streams.error) == 0
        assert len(actual) == 1
        assert isinstance(actual[0], psrpcore.types.PSSecureString)
        assert actual[0].decrypt() == "secret"
        read_line_as_secure_string.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_host_call_async(
    conn: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def read_line() -> str:
        await asyncio.sleep(0)
        return "line"

    ps_host = psrp.PSHost(ui=psrp.PSHostUI())
    monkeypatch.setattr(ps_host.ui, "read_line", read_line)

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp, host=ps_host)
        ps.add_script("$Host.UI.ReadLine()")

        actual = await ps.invoke()
        assert actual == ["line"]


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_complex_commands(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_command("Set-Variable").add_parameters(Name="string", Value="foo")
        ps.add_statement()

        ps.add_command("Get-Variable").add_parameter("Name", "string")
        ps.add_command("Select-Object").add_parameter("Property", ["Name", "Value"])
        ps.add_statement()

        ps.add_command("Get-Variable").add_argument("string").add_parameter("ValueOnly", True)
        ps.add_command("Select-Object")

        actual = await ps.invoke()
        assert len(actual) == 2
        assert isinstance(actual[0], psrpcore.types.PSObject)
        assert actual[0].Name == "string"
        assert actual[0].Value == "foo"
        assert actual[1] == "foo"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_input_as_iterable(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("begin { $i = 0 }; process { [PSCustomObject]@{Idx = $i; Value = $_}; $i++ }")

        actual = await ps.invoke([1, "2", 3])
        assert len(actual) == 3

        assert actual[0].Idx == 0
        assert actual[0].Value == 1
        assert actual[1].Idx == 1
        assert actual[1].Value == "2"
        assert actual[2].Idx == 2
        assert actual[2].Value == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_input_as_async_iterable(conn: str, request: pytest.FixtureRequest) -> None:
    async def my_iterable() -> t.AsyncIterator[int]:
        yield 1
        await asyncio.sleep(0)
        yield 2
        yield 3

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("begin { $i = 0 }; process { [PSCustomObject]@{Idx = $i; Value = $_}; $i++ }")

        actual = await ps.invoke(my_iterable())
        assert len(actual) == 3

        assert actual[0].Idx == 0
        assert actual[0].Value == 1
        assert actual[1].Idx == 1
        assert actual[1].Value == 2
        assert actual[2].Idx == 2
        assert actual[2].Value == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_input_with_secure_string(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("begin { $i = 0 }; process { [PSCustomObject]@{Idx = $i; Value = $_}; $i++ }")

        actual = await ps.invoke([psrpcore.types.PSSecureString("my secret")])
        assert len(actual) == 1

        assert actual[0].Idx == 0
        assert isinstance(actual[0].Value, psrpcore.types.PSSecureString)
        assert actual[0].Value.decrypt() == "my secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_unbuffered_input(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("begin { $i = 0 }; process { [PSCustomObject]@{Idx = $i; Value = $_}; $i++ }")

        actual = await ps.invoke([1, "2", 3], buffer_input=False)
        assert len(actual) == 3

        assert actual[0].Idx == 0
        assert actual[0].Value == 1
        assert actual[1].Idx == 1
        assert actual[1].Value == "2"
        assert actual[2].Idx == 2
        assert actual[2].Value == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_large_input_output(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        data = "a" * 1_048_576
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("begin { $args[0] }; process { $_ }").add_argument(data)

        actual = await ps.invoke([data])
        assert len(actual) == 2
        assert actual[0] == data
        assert actual[1] == data


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_invoke_input_with_failure(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    state_event = asyncio.Event()

    async def state_change(event: t.Any) -> None:
        state_event.set()

    async def input_gen() -> t.AsyncIterator[int]:
        await state_event.wait()
        yield 1
        yield 2
        yield 3

    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script(
            """
            [CmdletBinding()]
            param([Parameter(ValueFromPipeline)]$InputObject)

            begin {
                throw "failure msg"
            }
            process { $InputObject }
            end { "end" }
            """
        )
        ps.state_changed += state_change

        with pytest.raises(psrp.PipelineFailed, match="Pipeline failed while sending input: failure msg"):
            await ps.invoke(input_gen())


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_invoke_input_without_expecting_input(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    state_event = asyncio.Event()

    async def state_change(event: t.Any) -> None:
        state_event.set()

    async def input_gen() -> t.AsyncIterator[int]:
        await state_event.wait()
        yield 1
        yield 2
        yield 3

    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("done")
        ps.state_changed += state_change

        with pytest.raises(psrp.PipelineFailed, match="Pipeline ended while sending input: .*"):
            await ps.invoke(input_gen())


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_invoke_async(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("1; Start-Sleep -Seconds 1; 2")

        task = await ps.invoke_async()
        assert ps.state == psrpcore.types.PSInvocationState.Running
        actual = await task
        assert ps.state == psrpcore.types.PSInvocationState.Completed
        assert actual == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_invoke_async_on_complete(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("1; Start-Sleep -Seconds 1; 2")

        on_complete_event = asyncio.Event()

        async def on_complete():
            on_complete_event.set()

        task = await ps.invoke_async(completed=on_complete)
        await on_complete_event.wait()
        actual = await task

        assert actual == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_stop(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        out = psrp.AsyncPSDataCollection[t.Any]()
        out_received = asyncio.Event()

        async def wait_out(event: psrpcore.PSRPEvent) -> None:
            out_received.set()

        out.data_added += wait_out

        ps.add_script("1; Start-Sleep -Seconds 60; 2")

        task = await ps.invoke_async(output_stream=out)
        assert ps.state == psrpcore.types.PSInvocationState.Running
        await out_received.wait()
        await ps.stop()

        with pytest.raises(psrp.PipelineStopped, match="The pipeline has been stopped."):
            await task

        # Try again with explicit output to capture before the stop
        out = psrp.AsyncPSDataCollection[t.Any]()
        out_received = asyncio.Event()

        async def wait_out(event: psrpcore.PSRPEvent) -> None:
            out_received.set()

        out.data_added += wait_out

        task = await ps.invoke_async(output_stream=out)
        await out_received.wait()
        await ps.stop()

        with pytest.raises(psrp.PipelineStopped, match="The pipeline has been stopped."):
            await task

        assert out == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_stop_after_complete(conn: str, request: pytest.FixtureRequest) -> None:
    event = asyncio.Event()

    async def done() -> None:
        event.set()

    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        ps.add_script("1")
        task = await ps.invoke_async(completed=done)
        await event.wait()

        await ps.stop()
        actual = await task
        assert actual == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_stop_async(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        out = psrp.AsyncPSDataCollection[t.Any]()
        out_received = asyncio.Event()

        async def wait_out(event: psrpcore.PSRPEvent) -> None:
            out_received.set()

        out.data_added += wait_out

        ps.add_script("1; Start-Sleep -Seconds 60; 2")

        invoke_task = await ps.invoke_async(output_stream=out)
        await out_received.wait()

        stop_task = await ps.stop_async()
        await stop_task

        with pytest.raises(psrp.PipelineStopped, match="The pipeline has been stopped."):
            await invoke_task

        assert out == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_powershell_stop_async_on_completed(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)

        out = psrp.AsyncPSDataCollection[t.Any]()
        out_received = asyncio.Event()

        async def wait_out(event: psrpcore.PSRPEvent) -> None:
            out_received.set()

        out.data_added += wait_out

        ps.add_script("1; Start-Sleep -Seconds 60; 2")

        invoke_task = await ps.invoke_async(output_stream=out)
        await out_received.wait()

        on_stop_event = asyncio.Event()

        async def on_stop():
            on_stop_event.set()

        stop_task = await ps.stop_async(completed=on_stop)
        await on_stop_event.wait()
        await stop_task

        with pytest.raises(psrp.PipelineStopped, match="The pipeline has been stopped."):
            await invoke_task

        assert out == [1]


@pytest.mark.asyncio
async def test_powershell_connect(psrp_wsman: psrp.ConnectionInfo) -> None:
    event = asyncio.Event()

    async def fire_event(data: t.Any) -> None:
        event.set()

    rp: t.Optional[psrp.AsyncRunspacePool]
    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script(
            """
            $tmp = [System.IO.Path]::GetTempPath()
            $tmpFile = Join-Path $tmp ([System.IO.Path]::GetRandomFileName())
            $tmpFile

            while (-not (Test-Path -Path $tmpFile)) {
                Start-Sleep -Milliseconds 100
            }
            Remove-Item -Path $tmpFile
            "data in disconnection"
            """
        )

        out_stream = psrp.AsyncPSDataCollection[t.Any]()
        out_stream.data_added += fire_event

        task = await ps.invoke_async(output_stream=out_stream)
        await event.wait()
        event.clear()
        tmp_file = out_stream[0]

        await rp.disconnect()
        actual = await task
        assert actual == []

    rpid = rp._pool.runspace_pool_id
    pid = ps._pipeline.pipeline_id

    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_command("Set-Content").add_parameters(Path=tmp_file, Value="")
        await ps.invoke()

    rp = None
    async for disconnected_rp in psrp.AsyncRunspacePool.get_runspace_pools(psrp_wsman):
        if disconnected_rp._pool.runspace_pool_id == rpid:
            rp = disconnected_rp
            break

    assert rp is not None

    pipelines = rp.create_disconnected_power_shells()
    assert len(pipelines) == 1
    assert isinstance(pipelines[0], psrp.AsyncPowerShell)
    assert pipelines[0]._pipeline.pipeline_id == pid
    async with rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened

        ps = pipelines[0]
        actual = await ps.connect()
        assert actual == ["data in disconnection"]
        assert ps.state == psrpcore.types.PSInvocationState.Completed
        await rp.disconnect()

    rp = None
    async for disconnected_rp in psrp.AsyncRunspacePool.get_runspace_pools(psrp_wsman):
        if disconnected_rp._pool.runspace_pool_id == rpid:
            rp = disconnected_rp
            break

    assert rp is not None
    pipelines = rp.create_disconnected_power_shells()
    assert pipelines == []

    await rp.connect()
    await rp.close()

    # FIXME: Remove this once I've figure out why it can be Broken
    if rp.state == psrpcore.types.RunspacePoolState.Broken:
        assert str(rp._connection_error) == "abc"

    assert rp.state == psrpcore.types.RunspacePoolState.Closed


@pytest.mark.asyncio
async def test_powershell_connect_async(psrp_wsman: psrp.ConnectionInfo) -> None:
    event = asyncio.Event()

    async def fire_event(data: t.Any) -> None:
        event.set()

    rp: t.Optional[psrp.AsyncRunspacePool]
    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script(
            """
            $tmp = [System.IO.Path]::GetTempPath()
            $tmpFile = Join-Path $tmp ([System.IO.Path]::GetRandomFileName())
            $tmpFile

            while (-not (Test-Path -Path $tmpFile)) {
                Start-Sleep -Milliseconds 100
            }
            Remove-Item -Path $tmpFile
            "data in disconnection 1"

            while (-not (Test-Path -Path $tmpFile)) {
                Start-Sleep -Milliseconds 100
            }
            Remove-Item -Path $tmpFile
            "data in disconnection 2"

            while (-not (Test-Path -Path $tmpFile)) {
                Start-Sleep -Milliseconds 100
            }
            Remove-Item -Path $tmpFile
            "final data"
            """
        )

        out_stream = psrp.AsyncPSDataCollection[t.Any]()
        out_stream.data_added += fire_event

        task = await ps.invoke_async(output_stream=out_stream)
        await event.wait()
        event.clear()
        tmp_file = out_stream[0]

        await rp.disconnect()
        await task
        assert ps.state == psrpcore.types.PSInvocationState.Disconnected

    rpid = rp._pool.runspace_pool_id
    pid = ps._pipeline.pipeline_id

    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_command("Set-Content").add_parameters(Path=tmp_file, Value="")
        await ps.invoke()

    rp = None
    async for disconnected_rp in psrp.AsyncRunspacePool.get_runspace_pools(psrp_wsman):
        if disconnected_rp._pool.runspace_pool_id == rpid:
            rp = disconnected_rp
            break

    assert rp is not None

    pipelines = rp.create_disconnected_power_shells()
    assert len(pipelines) == 1
    assert isinstance(pipelines[0], psrp.AsyncPowerShell)
    assert pipelines[0]._pipeline.pipeline_id == pid
    async with rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened

        out_stream = psrp.AsyncPSDataCollection[t.Any]()
        out_stream.data_added += fire_event

        ps = pipelines[0]
        task = await ps.connect_async(output_stream=out_stream)

        await event.wait()
        event.clear()
        assert out_stream[0] == "data in disconnection 1"

        await rp.disconnect()
        await task
        assert ps.state == psrpcore.types.PSInvocationState.Disconnected

    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_command("Set-Content").add_parameters(Path=tmp_file, Value="")
        await ps.invoke()

    rp = None
    async for disconnected_rp in psrp.AsyncRunspacePool.get_runspace_pools(psrp_wsman):
        if disconnected_rp._pool.runspace_pool_id == rpid:
            rp = disconnected_rp
            break

    assert rp is not None

    pipelines = rp.create_disconnected_power_shells()
    assert len(pipelines) == 1
    assert isinstance(pipelines[0], psrp.AsyncPowerShell)
    assert pipelines[0]._pipeline.pipeline_id == pid
    async with rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened

        out_stream = psrp.AsyncPSDataCollection[t.Any]()
        out_stream.data_added += fire_event

        ps = pipelines[0]
        task = await ps.connect_async(output_stream=out_stream)

        await event.wait()
        event.clear()
        assert out_stream[0] == "data in disconnection 2"

        async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
            ps = psrp.AsyncPowerShell(rp)
            ps.add_command("Set-Content").add_parameters(Path=tmp_file, Value="")
            await ps.invoke()

        await event.wait()
        assert out_stream[1] == "final data"
        actual = await task
        assert actual == []

    assert rp.state == psrpcore.types.RunspacePoolState.Closed


@pytest.mark.asyncio
async def test_powershell_connect_async_completed(psrp_wsman: psrp.ConnectionInfo) -> None:
    event = asyncio.Event()

    async def fire_event(data: t.Optional[t.Any] = None) -> None:
        event.set()

    rp: t.Optional[psrp.AsyncRunspacePool]
    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script(
            """
            $tmp = [System.IO.Path]::GetTempPath()
            $tmpFile = Join-Path $tmp ([System.IO.Path]::GetRandomFileName())
            $tmpFile

            while (-not (Test-Path -Path $tmpFile)) {
                Start-Sleep -Milliseconds 100
            }
            Remove-Item -Path $tmpFile
            "final data"
            """
        )

        out_stream = psrp.AsyncPSDataCollection[t.Any]()
        out_stream.data_added += fire_event

        task = await ps.invoke_async(output_stream=out_stream)
        await event.wait()
        event.clear()
        tmp_file = out_stream[0]

        await rp.disconnect()
        await task
        assert ps.state == psrpcore.types.PSInvocationState.Disconnected

    rpid = rp._pool.runspace_pool_id
    pid = ps._pipeline.pipeline_id

    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_command("Set-Content").add_parameters(Path=tmp_file, Value="")
        await ps.invoke()

    rp = None
    async for disconnected_rp in psrp.AsyncRunspacePool.get_runspace_pools(psrp_wsman):
        if disconnected_rp._pool.runspace_pool_id == rpid:
            rp = disconnected_rp
            break

    assert rp is not None

    pipelines = rp.create_disconnected_power_shells()
    assert len(pipelines) == 1
    assert isinstance(pipelines[0], psrp.AsyncPowerShell)
    assert pipelines[0]._pipeline.pipeline_id == pid
    async with rp:
        assert rp.state == psrpcore.types.RunspacePoolState.Opened

        ps = pipelines[0]
        task = await ps.connect_async(completed=fire_event)

        async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
            ps = psrp.AsyncPowerShell(rp)
            ps.add_command("Set-Content").add_parameters(Path=tmp_file, Value="")
            await ps.invoke()

        await event.wait()

        actual = await task
        assert actual == ["final data"]

        assert ps.state == psrpcore.types.PSInvocationState.Completed

    assert rp.state == psrpcore.types.RunspacePoolState.Closed


@pytest.mark.asyncio
@pytest.mark.parametrize("conn", ["proc", "wsman"])
async def test_run_get_command_meta(conn: str, request: pytest.FixtureRequest) -> None:
    connection = request.getfixturevalue(f"psrp_{conn}")
    async with psrp.AsyncRunspacePool(connection) as rp:
        gcm = psrp.AsyncCommandMetaPipeline(
            rp,
            name="Get-*Item",
            command_type=psrpcore.types.CommandTypes.Cmdlet,
            namespace=["Microsoft.PowerShell.Management"],
            arguments=["env:"],
        )

        actual = await gcm.invoke()
        assert isinstance(actual[0], psrpcore.types.CommandMetadataCount)

        for data in actual[1:]:
            assert isinstance(data, psrpcore.types.PSObject)
            assert isinstance(data.Name, str)


@pytest.mark.asyncio
async def test_run_wsman_with_operation_timeout(psrp_wsman: psrp.WSManInfo) -> None:
    psrp_wsman.operation_timeout = 2
    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        # Unfortunately this test needs to run for a longer time to test out
        # the scenario
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("1; Start-Sleep -Seconds 5; 2")

        actual = await ps.invoke()
        assert actual == [1, 2]


@pytest.mark.asyncio
async def test_run_wsman_unhandled_exception_in_runspace(
    psrp_wsman: psrp.WSManInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_receive = psrp._winrs.WinRS.receive

    i = 0

    def mock_receive(self: psrp._winrs.WinRS, stream: str, command_id: t.Optional[uuid.UUID] = None) -> str:
        nonlocal i
        i += 1
        if i < 3:
            return original_receive(self, stream, command_id)

        raise Exception("unhandled exception")

    changed_event = asyncio.Event()

    async def state_changed(event: psrpcore.RunspacePoolStateEvent) -> None:
        if event.state == psrpcore.types.RunspacePoolState.Broken:
            changed_event.set()

    monkeypatch.setattr(psrp._winrs.WinRS, "receive", mock_receive)
    rp = psrp.AsyncRunspacePool(psrp_wsman)
    rp.state_changed += state_changed
    async with rp:
        await changed_event.wait()

        with pytest.raises(psrp.PSRPError, match="unhandled exception"):
            await rp.reset_runspace_state()

        assert rp.state == psrpcore.types.RunspacePoolState.Broken

    # To avoid affecting the other tests we make sure the pool is closed
    rp._pool.state = psrpcore.types.RunspacePoolState.Opened
    rp._connection_error = None
    await rp.close()


@pytest.mark.asyncio
async def test_run_wsman_unhandled_exception_in_pipeline(
    psrp_wsman: psrp.WSManInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_receive = psrp._winrs.WinRS.receive

    def mock_receive(self: psrp._winrs.WinRS, stream: str, command_id: t.Optional[uuid.UUID] = None) -> str:
        if command_id:
            raise Exception("unhandled exception")

        return original_receive(self, stream, command_id)

    monkeypatch.setattr(psrp._winrs.WinRS, "receive", mock_receive)
    async with psrp.AsyncRunspacePool(psrp_wsman) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script('"test"')

        with pytest.raises(Exception, match="unhandled exception"):
            await ps.invoke()


@pytest.mark.asyncio
async def test_out_of_proc_remote_failure():
    async def server_task(
        outgoing: "asyncio.Queue[bytes]",
        incoming: "asyncio.Queue[bytes]",
    ) -> None:
        server_pool = psrpcore.ServerRunspacePool()

        # Create
        data = await incoming.get()
        xml_data = ElementTree.fromstring(data)
        assert xml_data.tag == "Data"
        assert xml_data.text is not None
        assert xml_data.attrib.get("PSGuid", "") == str(uuid.UUID(int=0))

        payload: t.Optional[psrpcore.PSRPPayload] = psrpcore.PSRPPayload(
            base64.b64decode(xml_data.text),
            psrpcore.StreamType.default,
            None,
        )
        assert payload is not None
        server_pool.receive_data(payload)

        event = server_pool.next_event()
        assert isinstance(event, psrpcore.SessionCapabilityEvent)

        event = server_pool.next_event()
        assert isinstance(event, psrpcore.InitRunspacePoolEvent)

        event = server_pool.next_event()
        assert event is None

        payload = server_pool.data_to_send()
        assert payload is not None
        await outgoing.put(ps_data_packet(*payload))
        await outgoing.put(ps_guid_packet("DataAck"))

        # Command
        data = await incoming.get()
        xml_data = ElementTree.fromstring(data)
        assert xml_data.tag == "Command"
        assert xml_data.text is None
        assert xml_data.attrib.get("PSGuid", "") != str(uuid.UUID(int=0))
        pipe_id = uuid.UUID(xml_data.attrib["PSGuid"])

        server_pipe = psrpcore.ServerPipeline(server_pool, pipe_id)
        await outgoing.put(ps_guid_packet("CommandAck", pipe_id))

        data = await incoming.get()
        xml_data = ElementTree.fromstring(data)
        assert xml_data.tag == "Data"
        assert xml_data.text is not None
        assert xml_data.attrib.get("PSGuid", "") == str(pipe_id)

        payload = psrpcore.PSRPPayload(
            base64.b64decode(xml_data.text),
            psrpcore.StreamType.default,
            pipe_id,
        )
        assert payload is not None
        server_pool.receive_data(payload)

        event = server_pool.next_event()
        assert isinstance(event, psrpcore.CreatePipelineEvent)
        assert isinstance(server_pipe.metadata, psrpcore.PowerShell)
        assert len(server_pipe.metadata.commands) == 1
        assert server_pipe.metadata.commands[0].command_text == "test script"

        await outgoing.put(ps_guid_packet("DataAck", pipe_id))

        # Pretend a fatal error happened during execption
        await outgoing.put(b"Fatal error occurred\nwhile executing pipeline")

    incoming = asyncio.Queue()
    outgoing = asyncio.Queue()
    task = asyncio_create_task(server_task(incoming, outgoing))

    connection = CustomOutOfProcInfo(incoming, outgoing)
    async with psrp.AsyncRunspacePool(connection) as rp:
        ps = psrp.AsyncPowerShell(rp)
        ps.add_script("test script")

        expected = "Failed to parse response: Fatal error occurred\\nwhile executing pipeline"
        with pytest.raises(psrp.PSRPError, match=expected):
            await ps.invoke()

    await task
