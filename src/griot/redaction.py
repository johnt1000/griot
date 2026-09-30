"""Finds credential-looking values in text that is about to be indexed, and
replaces them.

What is indexed is embedded (on a paid profile: sent to an API) and then
returned verbatim by search to any agent that asks. A credential in a tracked
file, a commit message or a pull request body would travel both ways. Nothing
here decides whether a value is a REAL credential: it cannot know. It replaces
what has the shape of one with a marker and leaves the rest of the text
searchable, which costs nothing when the value was a fixture or a public key.

Precision over recall, on purpose: a detector that fires on ordinary code
would blank out exactly what people search for. So every detector is a known
token format or a quoted high-entropy literal next to a credential keyword.
What this does NOT find: a password written in prose, personal data, a secret
split across lines or encoded, and any format not listed. The protection for
those is not indexing the file at all (.gitignore)."""

import argparse
import math
import re
from typing import Callable, NamedTuple


class Detector(NamedTuple):
    rule: str
    pattern: re.Pattern
    # Which group holds the value to replace (0 = the whole match), and an
    # optional check on the match that must pass for it to count.
    group: int | str = 0
    accept: Callable[[re.Match], bool] | None = None


_MARKER_PREFIX = "[REDACTED:"


def marker(rule: str) -> str:
    return f"{_MARKER_PREFIX}{rule}]"


def _entropy(value: str) -> float:
    counts = {ch: value.count(ch) for ch in set(value)}
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_CONSTANT_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


def _looks_random(value: str) -> bool:
    """A value that was generated, not written: letters and digits mixed, no
    more predictable than a random string of its alphabet, and not a name. A
    name is what a person composes (`--color-brand-500`,
    `rails_master_key_2024`, `GRIOT_PRICE_PER_1M_TOKENS`, a UUID, a regex
    character class, text in another script); a generated value almost never
    holds two separate lowercase words."""
    if not value.isascii() or value.startswith("[") or _UUID.fullmatch(value) or _CONSTANT_NAME.fullmatch(value):
        return False
    words = [part for part in re.split(r"[-_./:]", value) if re.fullmatch(r"[a-z]{3,}", part)]
    # Digits in at least two separate places. An identifier carries one
    # number (`V1LocalObjectReference`, `createO200KSpecialTokenMap`,
    # `userSessionToken2Value`); a generated value has them scattered.
    digit_runs = len(re.findall(r"\d+", value))
    return (digit_runs >= 2 and any(ch.isalpha() for ch in value)
            and _entropy(value) >= 3.5 and len(words) < 2)


# Words that, AFTER the credential word in the name a value is assigned to,
# say the value is about a credential rather than being one: `token_env`
# names a variable, `api_key_header` a header, `password_hash` and `auth_sha`
# are digests, `token_url` is an address. Before the credential word they say
# nothing about the value: `ENV_API_KEY` and `FILE_TOKEN` are credentials.
_DESCRIPTORS = frozenset("""
    sha md5 hash hashed digest checksum uuid guid id ids name names header headers env path paths url uri
    file filename dir type types length len size min max prefix suffix field fields param params label regex
    pattern format expiry expires expire expiration ttl timeout endpoint count index version algorithm alg
    scheme ref
""".split())
# Words that, anywhere in the name, say the value is made up.
_MADE_UP = frozenset({"example", "sample", "dummy", "fake", "mock", "placeholder"})


def _words(identifier: str) -> list[str]:
    return [part.lower() for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", identifier)]


def _describes_a_credential(match: re.Match) -> bool:
    """Whether the name a value is assigned to says the value is not a
    credential. The pattern starts at the credential keyword; what precedes
    it in the identifier is read here, from the text, so the pattern itself
    never has to scan backwards."""
    text, start = match.string, match.start()
    begin = start
    while begin > max(0, start - 40) and (text[begin - 1].isalnum() or text[begin - 1] == "_"):
        begin -= 1
    before, after = _words(text[begin:start]), _words(match.group("tail"))
    if _MADE_UP & set(before + after):
        return True
    # `X_API_KEY_HEADER_VALUE`: the value OF the header is the key itself.
    if after and after[-1] in ("value", "val"):
        return False
    return bool(_DESCRIPTORS & set(after))


_INTERPOLATION = ("${", "{{", "<%", "$(", "://")


def _a_quoted_credential(match: re.Match) -> bool:
    value = match.group("value")
    return (_looks_random(value) and not any(mark in value for mark in _INTERPOLATION)
            and not _describes_a_credential(match))


def _an_unquoted_credential(match: re.Match) -> bool:
    value = match.group("value")
    return _looks_random(value) and not _describes_a_credential(match)


_PLACEHOLDER_PASSWORDS = frozenset({
    "password", "passwd", "pass", "pwd", "secret", "token", "changeme", "example", "redacted",
    "mypassword", "yourpassword", "xxx", "xxxx", "xxxxx"})


def _a_real_url_password(match: re.Match) -> bool:
    """Not a placeholder, not template syntax, and not just a word: a password
    someone chose or generated has a digit or a symbol in it."""
    value = match.group("value")
    return (not value.startswith(("$", "%", "*")) and not any(ch in value for ch in "[]<>{}")
            and value.lower() not in _PLACEHOLDER_PASSWORDS
            and any(not ch.isalpha() and ch not in "-_" for ch in value))


_KEYWORD = r"(?:api[_-]?key|apikey|secret|token|passwd|password|private[_-]?key|access[_-]?key|auth)"
_KEY_LABEL = r"[A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?"
_B64 = r"[A-Za-z0-9+/=]"
# A line break as it is, or as the two characters `\n` a JSON or YAML value
# holds it as (a backslash is not base64, so the two never overlap), with the
# indentation a YAML block, a heredoc or a string literal puts around it.
_INDENT = r"[ \t]{0,64}"
_LINE_BREAK = r"(?:\\[nr]|\r\n|\n|\r)"
_BREAKS = rf"(?:{_INDENT}{_LINE_BREAK}){{1,6}}{_INDENT}"
_MAYBE_BREAKS = rf"(?:{_INDENT}{_LINE_BREAK}){{0,6}}{_INDENT}"

# Order matters twice: a specific format goes before a general one that would
# also match it and name it wrongly, and the two generic assignment shapes go
# last so they only see what no format claimed.
DETECTORS: tuple[Detector, ...] = (
    # The key MATERIAL, not the header: text that only mentions
    # `-----BEGIN ... PRIVATE KEY-----` (a constant, a sentence, a scanner's own
    # pattern) has no base64 body on the lines after it and is left alone.
    # Optional armor headers (an encrypted PEM, a PGP block), then base64
    # lines at any indentation, then the END line if it is there. A key that
    # was cut off ends where its base64 ends, never at the end of the text.
    Detector("private-key", re.compile(
        rf"-----BEGIN {_KEY_LABEL}-----{_BREAKS}"
        rf"(?:[A-Za-z][A-Za-z-]{{0,30}}: [^\n\\]{{0,120}}{_BREAKS}){{0,6}}"
        # Base64 of key material has a digit, a `+` or a `/` within its first
        # line. A long word that merely follows the header has none.
        rf"(?=[A-Za-z=]{{0,63}}[0-9+/]){_B64}{{16,}}(?:{_BREAKS}{_B64}{{16,}}){{0,4000}}"
        # The last line of a key is usually short. It is taken only when the
        # END line follows: otherwise the first word after a cut-off key
        # would go with it.
        rf"(?:(?:{_BREAKS}{_B64}{{1,15}})?{_MAYBE_BREAKS}-----END {_KEY_LABEL}-----)?")),
    Detector("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    Detector("aws-secret-access-key", re.compile(
        r"(?i)aws_?secret_?access_?key\W{1,4}([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"), group=1),
    Detector("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{36,})\b")),
    Detector("gitlab-token", re.compile(r"\bgl(?:pat|rt|ptt|cbt|dt)-[A-Za-z0-9_-]{20,}")),
    Detector("slack-token", re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{10,}")),
    Detector("slack-webhook", re.compile(
        r"hooks\.slack\.com/services/(T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{20,})"), group=1),
    Detector("discord-webhook", re.compile(
        r"discord(?:app)?\.com/api/webhooks/(\d{15,}/[A-Za-z0-9_-]{50,})"), group=1),
    Detector("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    Detector("stripe-webhook-secret", re.compile(r"\bwhsec_[A-Za-z0-9]{32,}\b")),
    # Before the OpenAI shape, which would also match this one and name it wrongly.
    Detector("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}"), accept=lambda m: _looks_random(m.group(0))),
    Detector("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}"),
             accept=lambda m: _looks_random(m.group(0))),
    Detector("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    Detector("google-oauth-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{28}(?![A-Za-z0-9_-])")),
    Detector("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    Detector("pypi-token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}")),
    Detector("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    Detector("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")),
    Detector("digitalocean-token", re.compile(r"\bdo[opr]_v1_[a-f0-9]{64}\b")),
    Detector("shopify-token", re.compile(r"\bshp(?:at|ca|pa|ss)_[a-f0-9]{32}\b")),
    Detector("age-secret-key", re.compile(r"\bAGE-SECRET-KEY-1[A-Z0-9]{58}\b")),
    Detector("telegram-bot-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}(?![A-Za-z0-9_-])")),
    Detector("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    # A password equal to the user name (postgres:postgres, test:test) is the
    # default of a local service, not something to protect. The user part
    # takes no bracket, so a marker left by an earlier detector (a token used
    # AS the user) is not mistaken for one.
    Detector("url-password", re.compile(
        r"\b[a-z][a-z0-9+.-]{1,20}://([^\s/:@'\"\[\]]{1,64}):(?!\1@)(?P<value>[^\s/@'\"]{3,256})@"),
        group="value", accept=_a_real_url_password),
    Detector("bearer-token", re.compile(r"(?i)\bauthorization\b\W{1,6}bearer\s+([A-Za-z0-9._~+/=-]{20,})"),
             group=1, accept=lambda m: _looks_random(m.group(1))),
    Detector("basic-auth", re.compile(r"(?i)\bauthorization\b\W{1,6}basic\s+([A-Za-z0-9+/]{16,}={0,2})"), group=1),
    # Every quantifier in these two is bounded, and the keyword is not
    # preceded by "any identifier characters": an open-ended run on both sides
    # of the keyword made the pattern retry the same text for minutes on a
    # file of repeated identifier characters. The rest of the name is read by
    # _describes_a_credential() instead.
    Detector("credential-literal", re.compile(
        rf"(?i)(?P<key>{_KEYWORD})(?P<tail>[a-z0-9_]{{0,32}})['\"]?\s{{0,3}}[:=]\s{{0,3}}['\"](?P<value>[^\s'\"\\]{{16,512}})['\"]"),
        group="value", accept=_a_quoted_credential),
    # KEY=value with no quotes, as shell, YAML, compose and Terraform write
    # it. The most common way a real credential sits in a tracked file.
    Detector("credential-assignment", re.compile(
        rf"(?i)(?P<key>{_KEYWORD})(?P<tail>[a-z0-9_]{{0,32}})[ \t]{{0,3}}[:=][ \t]{{0,3}}"
        rf"(?P<value>[A-Za-z0-9+/=_~-]{{16,256}})(?![\w(.\[{{$<])"),
        group="value", accept=_an_unquoted_credential),
)


def redact(text: str) -> tuple[str, list[str]]:
    """`text` with every credential-looking value replaced by a marker, and
    the rule of each replacement, in order. Running it again on its own
    output changes nothing."""
    found: list[str] = []
    for detector in DETECTORS:
        def replace(match: re.Match, detector: Detector = detector) -> str:
            value = match.group(detector.group)
            # A marker left by an earlier detector, or by an earlier run, is
            # not a value: this is what makes redact() safe to run twice.
            if _MARKER_PREFIX in value or (detector.accept is not None and not detector.accept(match)):
                return match.group(0)
            found.append(detector.rule)
            start, end = match.span(detector.group)
            return match.string[match.start():start] + marker(detector.rule) + match.string[end:match.end()]
        text = detector.pattern.sub(replace, text)
    return text, found


def main(argv=None) -> int:
    """`griot audit`: where the index of the active profile holds
    credential-looking values. Read-only."""
    parser = argparse.ArgumentParser(
        prog="griot audit",
        description="Lists where the index holds credential-looking values: locations and rule names, never the "
                    "values. Read-only. Exit status 1 when something is found.",
    )
    parser.parse_args(argv)
    # Imported here: common imports this module for the detectors.
    import qdrant_edge as qe

    from griot import ask, common

    if not common.collection_exists(common.COLLECTION_NAME):
        print(f"Nothing is indexed for profile '{common.ACTIVE_PROFILE_NAME}'.")
        return 0
    client = common.get_client()
    places: dict[str, list[str]] = {}
    total, offset = 0, None
    while True:
        points, offset = client.scroll(qe.ScrollRequest(limit=1000, offset=offset, with_payload=True, with_vector=False))
        for point in points:
            payload = point.payload or {}
            rules = redact(payload.get("content") or "")[1]
            if rules:
                places.setdefault(common.shown(ask.source_label(payload)), []).extend(rules)
                total += len(rules)
        if offset is None:
            break
    if not places:
        print(f"Found nothing that looks like a credential in the index of profile '{common.ACTIVE_PROFILE_NAME}'.")
        return 0
    print(f"{total} credential-looking value(s) in {len(places)} place(s):")
    for where in sorted(places):
        print(f"  {where} ({', '.join(sorted(set(places[where])))})")
    print("Search already replaces them on the way out, and indexing a repository again rewrites its chunks "
          "without them.\nCheck whether each is real. One that is was stored here in plain text and, on a paid "
          "profile, sent to the embedding API when it was indexed: rotate it.")
    return 1
