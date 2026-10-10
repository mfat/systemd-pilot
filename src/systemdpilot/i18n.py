"""Translated strings, from a catalog looked up once.

gettext.gettext() searches the disk for the catalog on every call, and finds
nothing to keep when the language has no translation: building the services
list spent half its time there.
"""

import functools
import gettext

from . import GETTEXT_DOMAIN


@functools.cache
def _catalog() -> gettext.NullTranslations:
    # On first use, after the launcher has bound the domain to its locale directory.
    return gettext.translation(GETTEXT_DOMAIN, gettext.bindtextdomain(GETTEXT_DOMAIN), fallback=True)


def _(message: str) -> str:
    return _catalog().gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _catalog().ngettext(singular, plural, n)
