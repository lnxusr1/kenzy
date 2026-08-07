"""Which PyTorch device to use — one implementation, several callers.

``auto`` is a real, shipped config value (it is the packaged default for
``kokoro.device``), so *every* consumer has to resolve it before handing it to a
model. The TTS service did; the ``kenzy-setup`` pre-warm did not, and passed the
literal string ``"auto"`` to Kokoro — so the one code path whose entire job is
downloading and warming the model was the path doing it wrong, on a default
install.

The two also disagreed about the default itself (``auto`` in the service,
``cpu`` in setup), which meant a GPU host could pre-warm on CPU and then run on
CUDA. Living here, that can't drift again.
"""

from __future__ import annotations


def resolve_device(device: str) -> str:
    """Resolve ``"auto"`` to the best available PyTorch device.

    Anything else is passed through untouched — an operator who wrote ``cuda``
    means ``cuda``, and should get a real error rather than a silent CPU
    fallback if it isn't there.
    """
    if device != "auto":
        return device
    import torch  # type: ignore[import-untyped]

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"
