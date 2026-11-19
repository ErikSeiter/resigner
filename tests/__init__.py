import os
import tempfile

# resigner.config reads DATA_DIR when it is first imported, so it has to be set here, once per test run.
DATA = tempfile.mkdtemp(prefix="resigner-test-")
os.environ["DATA_DIR"] = DATA
