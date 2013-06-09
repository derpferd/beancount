"""OANDA transaction detail CSV file importer.

About the implementation [2013-06-09]:

- The 'xlrd' library won't work on these files; the files downloaded from the
  RBC website are in XML format, progid="Excel.Sheet". They are incredibly
  messy-- no way I'm going to waste time parsing the Microsoft XMl, srsly-- and
  xlrd does not grok them.

- An alternative, 'openpyxl', does not yet work with Python 3. I did not attempt
  to port it yet.

- The LibreOffice batch converter 'unoconv'... dumps core. Using batch
  LibreOffice does not work either: the following results in no file and an
  error message, despite the GUI being able to load them up:

    libreoffice --headless --convert-to csv ... --outdir ...

- Gnumeric has a command-line tool called 'ssconvert' that works to convert
  these files into CSV; this is what I do, and then use the CSV parser to get
  the job done. Install ssconvert to run this importer.

"""

import re
import datetime
import collections
import subprocess
import tempfile
from pprint import pprint

from beancount2.core import data
from beancount2.core.data import create_simple_posting
from beancount2.core.data import create_simple_posting_with_cost
from beancount2.core.data import Posting, Transaction, Check, Note, Decimal, Lot, Amount
from beancount2.core.data import FileLocation, format_entry, account_from_name
from beancount2.core.inventory import Position
from beancount2.core import compress
from beancount2 import utils
from beancount2.imports import filetype


ID = 'rbcinvesting'

INSTITUTION = ('RBC Direct Investing' , 'CA')

CONFIG_ACCOUNTS = {
    'application/vnd.ms-excel': {
        'FILE'            : 'Account for filing',
        'cash'            : 'Cash account',
        'positions'       : 'Root account for all position sub-accounts',
        'fees'            : 'Fees',
        'commission'      : 'Commissions',
        'interest'        : 'Interest income',
        'dividend'        : 'Dividend income',
        'transfer'        : 'Other account for inter-bank transfers',
    },
    'application/pdf' : {
        'FILE'               : 'Account for filing',
    },
}


def is_matching_file(contents, filetype):
    return (filetype == 'application/vnd.ms-excel' and
            re.search('Activity\d\d\d\d\d\d\d\d - \d\d\d\d\d\d\d\d', contents))


def import_file(filename, config, entries):
    if filetype.guess_file_type(filename) == 'application/vnd.ms-excel':
        return import_excel_file(filename, config, entries)


#--------------------------------------------------------------------------------

def import_excel_file(filename, config, entries):
    """Import an Excel file from RBC Direct Investing's Activity Statement."""

    print('----------------------------------------', filename)
    new_entries = []

    with tempfile.NamedTemporaryFile(suffix='.csv') as f:
        r = subprocess.call(('ssconvert', filename, f.name),
                            stdout=subprocess.PIPE)
        assert r == 0, r

        rdr = utils.csv_tuple_reader(open(f.name))
        for index, row in enumerate(rdr):
            row = fixup_row(row)
            # print(row)
            # print()

            # Gather transaction basics.
            fileloc = FileLocation(filename, index)

            # Ignore the settlement date if it is the same as the date.
            if row.settlement == row.date:
                settlement = None
            else:
                settlement = 'Settlement: {}'.format(row.settlement)

            # Gather the amount from the description; there is sometimes an other
            # amount in there, that doesn't show up in the downloaded file.
            mo = re.search(r'\$([0-9,]+\.[0-9]+)', row.description)
            description_amount = decimal(mo.group(1)) if mo else None

            # Gather the number of shares from the description. Sometimes
            # present as well.
            mo = re.search(r'\b([0-9]+) SHS', row.description)
            description_shares = decimal(mo.group(1)) if mo else None

            # Create a new transaction.
            narration = ' -- '.join(filter(None,
                                           [row.action, row.symbol, row.description, settlement]))
            entry = Transaction(fileloc, row.date, data.FLAG_IMPORT, None, narration, None, None, [])

            # Figure out an account for the position.
            if row.symbol:
                account_position = account_from_name('{}:{}'.format(config['positions'].name,
                                                                    row.symbol))

            # Add relevant postings.
            extra_narration = []
            if row.action == 'ADJ RR':
                pass

            elif row.action == 'RTC RR':
                pass

            elif row.action == 'EXH AB':
                pass

            elif row.action == 'DIV F6':
                assert description_amount

                create_simple_posting_with_cost(entry, account_position,
                                                row.quantity, row.symbol,
                                                description_amount, row.currency)
                create_simple_posting(entry, config['dividend'],
                                      -(row.quantity * description_amount), row.currency)

            elif row.action in ('Buy', 'Sell'):

                create_simple_posting_with_cost(entry, account_position,
                                                row.quantity, row.symbol,
                                                row.price, row.currency)

                create_simple_posting(entry, config['cash'],
                                      row.amount, row.currency)

            elif row.action in ('SEL FF', 'PUR FF'):
                assert not description_amount

                create_simple_posting(entry, account_position,
                                      row.quantity, row.symbol)

            elif row.action == 'DIST':
                assert not description_amount

                create_simple_posting(entry, config['dividend'],
                                      -row.amount, row.currency)
                create_simple_posting(entry, config['cash'],
                                      row.amount, row.currency)

                # Insert the otherwise unused price per-share in the
                # description.
                extra_narration.append('{} per share'.format(row.price))

            else:
                raise ValueError("Unknown action: '{}'".format(row.action))

            # if row.quantity:
            #     create_position_posting(entry, account_position, row)

            # if account_amount:
            #     data.create_simple_posting(entry, account_amount,
            #                                row.amount, row.currency)



            new_entries.append(entry)

    return new_entries


def fixup_row(row):
    """Fix up the row, parsign dates and converting amounts to decimal types,
    scaling amounts where necessary, and ensuring that there is a valid action
    on every row.
    """

    # Parse the dates.
    row = row._replace(
        date=datetime.datetime.strptime(row.date, '%Y-%m-%d').date(),
        settlement=datetime.datetime.strptime(row.settlement, '%Y-%m-%d').date())

    # Convert all amounts to decimal.
    row = row._replace(quantity=decimal(row.quantity),
                       price=decimal(row.price),
                       amount=decimal(row.amount))

    # If this is a transaction in 1000'ths amount, divide the quantity.
    if re.match('1000THS', row.description):
        row = row._replace(quantity=row.quantity / 1000)

    # Compute the amount, if not computed for us.
    if row.amount == '0':
        row = row._replace(amount=decimal(row.quantity) * decimal(row.price))

    # Figure how what the "action" of this row is.
    action = row.action
    if not action:
        if re.search(r'\bDIST\b', row.description):
            action = 'DIST'
        row = row._replace(action=action)
    assert action, row.description

    return row


# def create_position_posting(entry, account, row):
#     if row.price:
#         data.create_simple_posting_with_cost(entry, account,
#                                              row.quantity, row.symbol,
#                                              row.price, row.currency)
#     else:
#         data.create_simple_posting(entry, account,
#                                    row.quantity, row.symbol)


def decimal(strord):
    if isinstance(strord, Decimal):
        return strord
    else:
        assert isinstance(strord, str)
        if not strord:
            return Decimal()
        else:
            return Decimal(strord.replace(',', ''))
