# Wurlitzer

Capture C-level stdout/stderr pipes in Python via `os.dup2`.

For more details on why this is needed, please read [this blog post](https://eli.thegreenplace.net/2015/redirecting-all-kinds-of-stdout-in-python/).

## Install

    pip install wurlitzer

## Usage

Capture stdout/stderr in pipes:

```python
from wurlitzer import pipes

with pipes() as (out, err):
    call_some_c_function()

stdout = out.read()
```

Capture stdout/stderr in StringIO:

```python
from io import StringIO
from wurlitzer import pipes, STDOUT

out = StringIO()
with pipes(stdout=out, stderr=STDOUT):
    call_some_c_function()

stdout = out.getvalue()
```

Forward C-level stdout/stderr to Python sys.stdout/stderr,
which may already be forwarded somewhere by the environment, e.g. IPython:

```python
from wurlitzer import sys_pipes

with sys_pipes():
    call_some_c_function()
```

Forward C-level output to Python Logger objects (new in 3.1).
Each line of output will be a log message.

```python
from wurlitzer import pipes, STDOUT
import logging

logger = logging.getLogger("my.log")
logger.setLevel(logging.INFO)
logger.addHandler(logging.FileHandler("mycode.log"))

with pipes(logger, stderr=STDOUT):
    call_some_c_function()
```

Or even simpler, enable it as an IPython extension:

```
%load_ext wurlitzer
```

To forward all C-level output to IPython during execution.

## Acknowledgments

This package is based on stuff we learned with @takluyver and @karies while working on capturing output from the [Cling Kernel](https://github.com/root-mirror/cling/tree/master/tools/Jupyter/kernel) for Jupyter.

## Wurlitzer?!

[Wurlitzer](https://en.wikipedia.org/wiki/Wurlitzer) makes pipe organs. Get it? Pipes? Naming is hard.
