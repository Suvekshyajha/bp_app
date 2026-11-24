import subprocess
import sys


def test_importing_embeddings_does_not_load_any_model():
    """The orchestrator and the ingestion API must be able to import
    common.embeddings without pulling in torch or downloading a model."""

    code = (
        "import sys, common.embeddings;"
        "bad = [m for m in ('torch', 'sentence_transformers', 'fastembed')"
        " if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )

    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
