"""Text a Postgres column can actually hold (D56).

`text` and `varchar` in Postgres cannot store a NUL byte -- and a client can put
one in any JSON string, because `\\u0000` is a valid escape. The insert then
raises a `DataError`, which is **not** an `IntegrityError`, escapes every
handler, and comes back as a `500`.

Same family as the id wider than `integer` and the over-long filename: **a value
the client controls that the column does not expect.** And the same answer:
refuse it at the edge, once, in a type every free-text field shares -- so a field
added later inherits the guard instead of having to remember it.

The guard is not cosmetic. `POST /auth/register` takes free text and needs no
token, so before this the whole API had an unauthenticated `500`.
"""

from typing import Annotated

from pydantic import AfterValidator, Field

NUL = "\x00"


def _without_nul(value: str) -> str:
    if NUL in value:
        # Refused, not stripped. Silently removing a byte stores a value that is
        # not the value that was sent -- the same reason an over-long filename is
        # refused rather than truncated (D25).
        raise ValueError("must not contain a NUL byte")
    return value


SafeText = Annotated[str, AfterValidator(_without_nul)]


def bounded_text(*, min_length: int = 1, max_length: int) -> object:
    """A free-text field with a ceiling and no NUL."""
    return Annotated[SafeText, Field(min_length=min_length, max_length=max_length)]
