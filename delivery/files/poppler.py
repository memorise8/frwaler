"""Bound the local fallback extractor; executed inside the isolated text stage."""
import os
import resource
import sys

from .text_quality import MAX_TEXT_BYTES


def main():
    if len(sys.argv) != 3:
        raise SystemExit(2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_TEXT_BYTES, MAX_TEXT_BYTES))
    resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    os.execvp('pdftotext', ['pdftotext', '-enc', 'UTF-8', '-eol', 'unix',
                          sys.argv[1], sys.argv[2]])


if __name__ == '__main__':
    main()
