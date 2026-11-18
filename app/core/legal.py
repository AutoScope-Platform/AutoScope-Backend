"""Legal wording shown by more than one module.

FSD 5.7: insurance flows must say clearly that AutoScope only connects people with
insurers and that the listed, licensed insurer underwrites the policy.
"""

from __future__ import annotations

INSURANCE_DISCLOSURE = (
    "AutoScope is a marketplace that connects you with licensed insurers. AutoScope does not "
    "underwrite, issue or guarantee insurance. Cover, premiums and claims are decided by the "
    "insurer named on the offer or policy, which is responsible for it."
)


def insurance_disclosure(insurer_name: str | None = None) -> str:
    """The disclosure, naming the insurer when we know it (certificates, offers)."""
    if not insurer_name:
        return INSURANCE_DISCLOSURE
    return (
        "AutoScope is a marketplace that connects you with licensed insurers. AutoScope does not "
        f"underwrite, issue or guarantee insurance. This policy is underwritten by {insurer_name}, "
        "which is responsible for cover, premiums and claims."
    )
