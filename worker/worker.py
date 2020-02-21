import argparse
import traceback
import asyncio
import signal
from os import getpid
from typing import Any, Dict
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
from aiortc.contrib.media import MediaPlayer, MediaStreamTrack
from channel import Request, Notification, Channel
from handler import Handler
from logger import Logger

# File descriptors to communicate with the Node.js process
READ_FD = 3
WRITE_FD = 4


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="aiortc mediasoup-client handler")
    parser.add_argument(
        "--logLevel", "-l", choices=["debug", "warn", "error", "none"])
    args = parser.parse_args()

    """
    Argument handling
    """
    if args.logLevel and args.logLevel != "none":
        Logger.setLogLevel(args.logLevel)

    Logger.debug("starting mediasoup-client aiortc worker")

    """
    Initialization
    """
    # dictionary of players indexed by id
    players: Dict[str, MediaPlayer] = ({})
    # dictionary of handlers indexed by id
    handlers: Dict[str, Handler] = ({})

    # run event loop
    loop = asyncio.get_event_loop()

    # create channel
    channel = Channel(loop, READ_FD, WRITE_FD)

    def shutdown() -> None:
        loop.close()

    def getHandler(handlerId: str) -> Handler:
        return handlers[handlerId]

    def getTrack(playerId: str, kind: str) -> MediaStreamTrack:
        player = players[playerId]
        track = player.audio if kind == "audio" else player.video
        if not track:
            raise Exception("no track found")

        return track

    async def processRequest(request: Request) -> Any:
        Logger.debug(
            f"processRequest() [method:{request.method}, internal:{request.internal}, data:{request.data}]"
        )

        if request.method == "createHandler":
            internal = request.internal
            handlerId = internal["handlerId"]
            data = request.data

            # use RTCConfiguration if given
            jsonRtcConfiguration = data.get("rtcConfiguration")
            rtcConfiguration = None

            if jsonRtcConfiguration and "iceServers" in jsonRtcConfiguration:
                iceServers = []
                for entry in jsonRtcConfiguration["iceServers"]:
                    iceServer = RTCIceServer(
                        urls=entry.get("urls"),
                        username=entry.get("username"),
                        credential=entry.get("credential"),
                        credentialType=entry.get("credentialType")
                    )
                    iceServers.append(iceServer)
                rtcConfiguration = RTCConfiguration(iceServers)

            handler = Handler(handlerId, channel, loop, getTrack, rtcConfiguration)

            handlers[handlerId] = handler
            return

        elif request.method == "createPlayer":
            internal = request.internal
            playerId = internal["playerId"]
            data = request.data
            player = MediaPlayer(
                data["file"],
                data["format"] if "format" in data else None,
                data["options"] if "options" in data else None
            )

            players[playerId] = player
            return

        elif request.method == "getRtpCapabilities":
            pc = RTCPeerConnection()
            pc.addTransceiver("audio", "sendonly")
            pc.addTransceiver("video", "sendonly")
            offer = await pc.createOffer()
            await pc.close()
            return offer.sdp

        else:
            internal = request.internal
            handler = getHandler(internal["handlerId"])
            return await handler.processRequest(request)

    async def processNotification(notification: Notification) -> None:
        Logger.debug(
            f"processNotification() [event:{notification.event}, internal:{notification.internal}, data:{notification.data}]"
        )

        if notification.event == "handler.close":
            internal = notification.internal
            handlerId = internal["handlerId"]
            handler = handlers[handlerId]

            handler.close()

            del handlers[handlerId]

        elif notification.event == "player.close":
            internal = notification.internal
            playerId = internal["playerId"]
            player = players[playerId]

            if player.audio:
                player.audio.stop()
            if player.video:
                player.video.stop()

            del players[playerId]

        elif notification.event == "player.stopTrack":
            internal = notification.internal
            playerId = internal["playerId"]
            player = players[playerId]
            data = notification.data
            kind = data["kind"]

            if kind == "audio" and player.audio:
                player.audio.stop()
            elif kind == "video" and player.video:
                player.video.stop()

        else:
            internal = notification.internal
            handler = getHandler(internal["handlerId"])
            return await handler.processNotification(notification)

    async def run(channel: Channel) -> None:
        # tell the Node process that we are running
        await channel.notify(str(getpid()), "running")

        while True:
            try:
                obj = await channel.receive()

                if obj is None:
                    continue

                elif "method" in obj:
                    request = Request(**obj)
                    request.setChannel(channel)
                    try:
                        result = await processRequest(request)
                        await request.succeed(result)
                    except Exception as error:
                        errorStr = f"{error.__class__.__name__}: {error}"
                        Logger.error(
                            f"request '{request.method}' failed: {errorStr}"
                        )
                        if not isinstance(error, TypeError):
                            traceback.print_tb(error.__traceback__)
                        await request.failed(error)

                elif "event" in obj:
                    notification = Notification(**obj)
                    try:
                        await processNotification(notification)
                    except Exception as error:
                        errorStr = f"{error.__class__.__name__}: {error}"
                        Logger.error(
                            f"notification '{notification.event}' failed: {errorStr}"
                        )
                        if not isinstance(error, TypeError):
                            traceback.print_tb(error.__traceback__)

            except Exception:
                break

    # signal handler
    loop.add_signal_handler(signal.SIGINT, shutdown)
    loop.add_signal_handler(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(
            run(channel)
        )
    # reached after calling loop.stop() or channel failure
    except RuntimeError:
        pass
    finally:
        # TODO: we force loop closure, otherwise RTCPeerConnection may not close
        # and we may end up with a zoombie process
        loop.close()
        # TODO: Ideally we should gracefully close instances as follows
        # loop.run_until_complete(handler.close())
        # loop.run_until_complete(channel.close())
