#!/usr/bin/env python
import sys

from setuptools import setup
from setuptools.command.bdist_egg import bdist_egg

with open("README.md") as f:
    long_description = f.read()

version_ns = {}
with open("wurlitzer.py") as f:
    for line in f:
        if line.startswith("__version__"):
            exec(line, version_ns)


class bdist_egg_disabled(bdist_egg):
    """Disabled version of bdist_egg

    Prevents setup.py install from performing setuptools' default easy_install,
    which it should never ever do.
    """

    def run(self):
        sys.exit(
            "Aborting implicit building of eggs. Use `pip install .` to install from source."
        )


setup_args = dict(
    name="wurlitzer",
    version=version_ns["__version__"],
    author="Min RK",
    author_email="benjaminrk@gmail.com",
    description="Capture C-level output in context managers",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/minrk/wurlitzer",
    py_modules=["wurlitzer"],
    python_requires=">=3.6",
    license="MIT",
    cmdclass={
        "bdist_egg": bdist_egg if "bdist_egg" in sys.argv else bdist_egg_disabled
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
    ],
)

if __name__ == "__main__":
    setup(**setup_args)
