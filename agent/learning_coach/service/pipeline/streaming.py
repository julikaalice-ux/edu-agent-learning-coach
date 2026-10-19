"""Showing the coordinator's reply while it is written.

Only the text of the reply's "message" field is passed on, never the JSON
around it. The teacher opens a stream only where the program is unlikely to
replace the draft: no question waiting for an answer, no practice route, no
early explanation already shown. The stream stops for good once the text
being written no longer continues what was shown (for example a format retry
that wrote something else). The finished reply is still checked as a whole;
when it does not continue the shown text, the host adds it after that text.
"""

from contextlib import contextmanager
import logging

from learning_coach.domain.partial_reply import partial_message
from learning_coach.service.execution import REPLY_SINK

LOG = logging.getLogger(__name__)


class ReplyStream:
    def __init__(self, emit, *, allow=None):
        # emit(piece): new reply text for the learner. allow(head): whether a
        # reply whose JSON starts with head may be shown while it is written.
        self.emit, self.allow = emit, allow
        self.shown = ""
        self.closed = False

    def __call__(self, raw: str) -> None:
        if self.closed:
            return
        try:
            text, head = partial_message(raw)
            if text is None:
                return
            if not self.shown and self.allow is not None and not self.allow(head):
                self.closed = True
                return
            if text.startswith(self.shown):
                piece = text[len(self.shown):]
                if piece:
                    self.shown = text
                    self.emit(piece)
            elif not self.shown.startswith(text):
                # A new attempt wrote something else; the rest comes with the final reply.
                self.closed = True
        except Exception:  # showing text early must never break the call itself
            LOG.warning("reply_stream_failed")
            self.closed = True


@contextmanager
def streaming(stream):
    """Let the runner stream the coordinator call made inside this block (None: no streaming)."""
    if stream is None:
        yield
        return
    token = REPLY_SINK.set(stream)
    try:
        yield
    finally:
        REPLY_SINK.reset(token)
