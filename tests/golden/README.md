# Golden statements

One sample export per bank and the parse result it must produce.

**These files are synthetic.** They follow each bank's export layout as published and as commonly seen,
with invented names, UPI IDs and UTRs. Before an adapter is trusted, replace its file here with a real,
anonymised export from that bank and regenerate the expected output:

    PAYSLEUTH_UPDATE_GOLDEN=1 python -m pytest tests/test_banks.py

then read the diff of the `.expected.json` file line by line before committing it.
