import re

from ..audit.service import redact_sensitive


def redact_output(value):
    if isinstance(value, str):
        value = re.sub(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
                       "[REDACTED private key]", value, flags=re.S)
        # Treat the whole credential-bearing IOS/ASA line as sensitive, including
        # numbered/typed password hashes that a one-token substitution would miss.
        value = re.sub(r"(?im)^.*\b(?:password|secret|community|key|private-key|pre-shared-key|key-string|authentication-key|encryption-key)\b.*$",
                       "[REDACTED credential line]", value)
        return redact_sensitive(value)
    if isinstance(value, dict):
        return {key: redact_output(item) for key, item in redact_sensitive(value).items()}
    if isinstance(value, list):
        return [redact_output(item) for item in value]
    return value
