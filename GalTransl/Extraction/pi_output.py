"""Bounded streaming tail and Pi shell result formatting; logs retain all bytes."""
import codecs

from .pi_text import MAX_BYTES, MAX_LINES, format_size, truncate


class OutputAccumulator:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")("replace")
        self.tail = ""
        self.boundary = True
        self.total_bytes = self.completed_lines = self.last_line_bytes = 0

    def append(self, data, final=False):
        text = self.decoder.decode(data, final=final)
        self.total_bytes += len(text.encode("utf-8"))
        self.completed_lines += text.count("\n")
        if "\n" in text:
            self.last_line_bytes = len(text.rsplit("\n", 1)[-1].encode("utf-8"))
        else:
            self.last_line_bytes += len(text.encode("utf-8"))
        self.tail += text
        raw = self.tail.encode("utf-8")
        if len(raw) > MAX_BYTES * 4:
            start = len(raw) - MAX_BYTES * 2
            while start < len(raw) and raw[start] & 0xc0 == 0x80:
                start += 1
            self.boundary = raw[start - 1] == 10
            self.tail = raw[start:].decode("utf-8")

    def snapshot(self):
        text = self.tail
        if not self.boundary and "\n" in text:
            text = text.split("\n", 1)[1]
        result = truncate(text, tail=True)
        total_lines = self.completed_lines + bool(self.last_line_bytes)
        cut = total_lines > MAX_LINES or self.total_bytes > MAX_BYTES
        result.update(truncated=cut, totalLines=total_lines, totalBytes=self.total_bytes)
        if cut and result["truncatedBy"] is None:
            result["truncatedBy"] = "bytes" if self.total_bytes > MAX_BYTES else "lines"
        return result


def shell_result(accumulator, path, exit_code, seconds, failure=""):
    snapshot = accumulator.snapshot()
    text = snapshot["content"] or ("" if failure else "(no output)")
    details = None
    if snapshot["truncated"]:
        details = {"truncation": snapshot, "fullOutputPath": str(path)}
        last = snapshot["totalLines"]
        first = last - snapshot["outputLines"] + 1
        if snapshot["lastLinePartial"]:
            text += f"\n\n[Showing last {format_size(snapshot['outputBytes'])} of line {last} (line is {format_size(accumulator.last_line_bytes)}). Full output: {path}]"
        else:
            budget = f" ({format_size(MAX_BYTES)} limit)" if snapshot["truncatedBy"] == "bytes" else ""
            text += f"\n\n[Showing lines {first}-{last} of {last}{budget}. Full output: {path}]"
    if failure or exit_code:
        status = failure or f"Command exited with code {exit_code}"
        text += ("\n\n" if text else "") + status
    if failure:
        return text
    size = path.stat().st_size
    cap = 1024 * 1024
    with path.open("rb") as stream:
        if size <= cap:
            full = stream.read().decode("utf-8-sig", errors="replace")
        else:
            head = codecs.getincrementaldecoder("utf-8-sig")("replace").decode(stream.read(cap // 2))
            stream.seek(-cap // 2, 2)
            tail = stream.read()
            start = 0
            while start < len(tail) and tail[start] & 0xc0 == 0x80:
                start += 1
            full = f"{head}\n\n[... {size - cap} bytes omitted ...]\n\n{tail[start:].decode('utf-8', errors='replace')}"
    structured = dict(output=full, truncated=size > cap, exit_code=exit_code,
                      wall_time_seconds=round(seconds, 1))
    if size > cap:
        structured["full_output_path"] = str(path)
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured,
            **({"details": details} if details else {}), **({"isError": True} if exit_code else {})}
