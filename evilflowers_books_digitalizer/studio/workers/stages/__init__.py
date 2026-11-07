"""Task bodies, one module per Celery task, free of Celery so they test without a broker.

Each stage reads what it needs in a short unit of work, does the slow part
(subprocesses, models) outside any transaction, and writes the result in a second
short unit — DB locks are never held across ScanTailor or Tesseract.
"""
