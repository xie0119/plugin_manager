"""Assemble one completed Shell result from MTU-sized 0x7A notifications."""
import struct

from ..btotatool.transport import TransportError


class PushOutput:
    def __init__(self, session):
        self.session = session
        self.code = self.length = None
        self.truncated = False
        self.output = bytearray()

    @property
    def complete(self):
        return self.length is not None and len(self.output) == self.length

    def feed(self, frame):
        if len(frame) < 4 or frame[0] != 0x7A:
            raise TransportError("Shell 推送帧头异常")
        if struct.unpack_from('<H', frame, 2)[0] != self.session:
            return
        if frame[1] == 0x80:
            if len(frame) != 11:
                raise TransportError("Shell 结果头长度异常")
            code, length, flags = struct.unpack_from('<iHB', frame, 4)
            if length > 512 or flags not in (1, 3):
                raise TransportError("Shell 返回状态或输出长度异常")
            self.code, self.length, self.truncated = code, length, bool(flags & 2)
            self.output.clear()  # A requested replay starts with a fresh result header.
        elif frame[1] == 0x81:
            if len(frame) < 7 or self.length is None:
                raise TransportError("Shell 输出缺少结果头或数据")
            offset = struct.unpack_from('<H', frame, 4)[0]
            block = frame[6:]
            if offset + len(block) > self.length or offset > len(self.output):
                raise TransportError("Shell 输出偏移不连续或超出总长度")
            if offset < len(self.output):
                if self.output[offset:offset + len(block)] != block:
                    raise TransportError("Shell 重复输出内容不一致")
                return
            self.output.extend(block)
        else:
            raise TransportError("未知 Shell 推送类型")
