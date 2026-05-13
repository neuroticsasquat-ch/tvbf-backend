"""Provider interface + shared exception type for the email module."""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmailSendError(Exception):
    """A send failed at the transport layer. Callers should log and continue."""


class EmailClient(ABC):
    @abstractmethod
    async def send(
        self,
        *,
        to: str,
        subject: str,
        html: str,
        text: str,
    ) -> None:
        """Send a single email. Raises EmailSendError on transport failure."""
