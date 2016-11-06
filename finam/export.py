# coding: utf-8
import datetime
import logging
import operator
import collections
from enum import IntEnum
from cStringIO import StringIO

try:
    from urllib import urlopen, urlencode
except ImportError:
    from urllib.request import urlopen
    from urllib.parse import urlencode

import pandas as pd
from pandas.parser import CParserError

__all__ = ['Market',
           'Period',
           'FinamDownloadError',
           'FinamParsingError',
           'FinamTooLongPeriodError',
           'FinamObjectNotFoundError',
           'Exporter']

logger = logging.getLogger()


class Market(IntEnum):

    """
    Markets mapped to ids used by finam.ru export
    """

    BONDS = 2
    CURRENCIES = 45
    ETF = 515
    FUTURES = 14  # non-expired futures
    FUTURES_ARCHIVE = 17  # expired futures
    INDEXES = 6
    SHARES = 1
    USA = 25


class Period(IntEnum):

    # TODO: < daily data support
    TICKS = 1
    MINUTES1 = 2
    MINUTES5 = 3
    MINUTES10 = 4
    MINUTES15 = 5
    MINUTES30 = 6
    HOURLY = 7
    DAILY = 8
    WEEKLY = 9
    MONTHLY = 10


class LookupComparator(IntEnum):

    EQUALS = 1
    STARTSWITH = 2
    CONTAINS = 3


class FinamExportError(Exception):
    pass


class FinamDownloadError(FinamExportError):
    pass


class FinamParsingError(FinamExportError):
    pass


class FinamObjectNotFoundError(FinamExportError):
    pass


class FinamTooLongPeriodError(FinamExportError):
    pass


class ExporterMeta(object):

    FINAM_DICT_URL = 'http://www.finam.ru/cache/icharts/icharts.js'
    FINAM_CHARSET = 'cp1251'
    FINAM_CATEGORIES = -1

    def __init__(self, lazy=True):
        self._meta = None
        if not lazy:
            self.__maybe_load()

    def __parse_js_assignment(self, line):
        """
        Parses 1-line js assignment used by finam.ru

        Can be either (watch spaces between commas)
        var string_arr = ['string1','string2',...,'stringN'] or
        var ints_arr = [int1,...,intN]

        May also contain empty strings ['abc','','']
        """
        logger.debug('Parsing line starting with "{}"'.format(line[:20]))

        # extracting everything between array brackets
        start_char, end_char = '[', ']'
        start_idx = line.find(start_char)
        end_idx = line.find(end_char)
        if (start_idx == -1 or
                end_idx == -1):
            raise FinamParsingError('Unable to parse line: {}'.format(line))
        items = line[start_idx + 1:end_idx]

        # string items
        if items.startswith("'"):
            # it may contain ',' inside lines so cant split by ','
            # i.e. "GILEAD SCIENCES, INC."
            items = items.split("','")
            for i in (0, -1):
                items[i] = items[i].strip("'")
            return items

        # int items
        return items.split(',')

    def __parse(self, data):
        """
        Parses js file used by finam.ru export tool
        """
        cols = ('id', 'name', 'code', 'market')
        parsed = dict()
        for idx, col in enumerate(cols[:len(cols)]):
            parsed[col] = self.__parse_js_assignment(data[idx])
        df = pd.DataFrame(columns=cols, data=parsed)
        df['market'] = df['market'].astype(int)
        # junk data + non-int ids, we don't need it
        df = df[df.market != self.FINAM_CATEGORIES]
        # now we can coerce ids to ints
        df['id'] = df['id'].astype(int)
        df.set_index('id', inplace=True)
        df.sort('market', inplace=True)
        return df

    def _fetch(self):
        """
        Just fetches finam's metadata
        """
        logger.info('Fetching metadata from {}'.format(self.FINAM_DICT_URL))
        try:
            return urlopen(self.FINAM_DICT_URL).readlines()
        except IOError as e:
            raise FinamDownloadError('Unable to load contracts dictionary: {}'
                                     .format(e.message))

    def __decode_data(self, data):
        """
        Converts finam's charset to utf8
        """
        logger.info('Decoding response from {}'.format(self.FINAM_CHARSET))
        try:
            return [line.decode(self.FINAM_CHARSET) for line in data]
        except LookupError:
            raise FinamExportError('No charset "{}" available'
                                   .format(self.FINAM_CHARSET))
        except UnicodeDecodeError as e:
            raise FinamExportError('Unable to decode dictionary content: {}'
                                   .format(e.message))

    def __maybe_load(self):
        """
        Downloads and parses finam's metadata if it's not done yet
        """
        if self._meta is not None:
            return

        rawdata = self._fetch()
        decoded = self.__decode_data(rawdata)
        self._meta = self.__parse(decoded)

    @property
    def meta(self):
        # so that dataframe can't be modified externally
        return self._meta.copy(deep=True)

    def __apply_filter(self, col, val, comparator):
        """
        Builds a dataframe matching original dataframe with conditions passed

        The original dataframe is left intact
        """
        if (not isinstance(val, collections.Container)
                or isinstance(val, basestring)):
            val = [val]

        if comparator == LookupComparator.EQUALS:
            # index must be sliced differently
            if col == 'id':
                expr = self._meta.index.isin(val)
            else:
                expr = self._meta[col].isin(val)
        else:
            if comparator == LookupComparator.STARTSWITH:
                op = 'startswith'
            else:
                op = 'contains'
            expr = self.__combine_filters(
                map(getattr(self._meta[col].str, op), val), operator.or_)
        return expr

    def __combine_filters(self, filters, op):
        result = filters[0]
        for filter_ in filters[1:]:
            result = op(result, filter_)
        return result

    def lookup(self, id_=None, code=None, market=None,
               code_comparator=LookupComparator.EQUALS):
        """
        Looks up contracts matching specified combinations of requirements
        If multiple requirements are specified they will be ANDed

        Note that the same id can have multiple matches as an entry
        may appear in different markets
        """
        if not any((id_, code, market)):
            raise ValueError('Either id or code or market must be specified')

        self.__maybe_load()
        filters = []

        # applying filters
        filter_groups = (('id', id_, LookupComparator.EQUALS),
                         ('code', code, code_comparator),
                         ('market', market, LookupComparator.EQUALS))

        for col, val, comparator in filter_groups:
            if val is not None:
                filters.append(self.__apply_filter(col, val, comparator))

        combined_filter = self.__combine_filters(filters, operator.and_)
        res = self._meta[combined_filter]
        if len(res) == 0:
            raise FinamObjectNotFoundError
        return res


class Exporter(object):

    DEFAULT_EXPORT_HOST = 'export.finam.ru'
    IMMUTABLE_PARAMS = {
        'd': 'd',
        'f': 'table',
        'e': '.csv',
        'dtf': '1',
        'tmf': '3',
        'MSOR': '0',
        'mstime': 'on',
        'mstimever': '1',
        'sep': '3',
        'sep2': '1',
        'at': '1'
    }

    ERROR_TOO_MUCH_WANTED = (u'Вы запросили данные за слишком '
                             u'большой временной период')

    def __init__(self, export_host=None):
        self._meta = ExporterMeta(lazy=True)
        if export_host is not None:
            self._export_host = export_host
        else:
            self._export_host = self.DEFAULT_EXPORT_HOST

    def __build_url(self, params):
        url = ('http://{}/table.csv?{}&{}'
               .format(self._export_host,
                       urlencode(self.IMMUTABLE_PARAMS),
                       urlencode(params)))
        return url

    def __do_sanity_checks(self, data):
        if self.ERROR_TOO_MUCH_WANTED.encode('cp1251') in data:
            raise FinamTooLongPeriodError

        if not all(c in data for c in '<>;'):
            raise FinamParsingError('Returned data doesnt seem like '
                                    'a valid csv dataset')

    def _fetch(self, url):
        logger.info('Loading data from {}'.format(url))
        try:
            return urlopen(url).read()
        except IOError as e:
            raise FinamDownloadError('Unable to download {}: {}'
                                     .format(url, e.message))

    def lookup(self, *args, **kwargs):
        return self._meta.lookup(*args, **kwargs)

    def download(self,
                 id_,
                 market,
                 start_date=datetime.date(2007, 1, 1),
                 end_date=None,
                 period=Period.DAILY):

        items = self._meta.lookup(id_=id_, market=market)
        # i.e. for markets 91, 519, 2
        # id duplicates are feasible, looks like corrupt data on finam
        # can do nothing about it but inform the user
        if len(items) != 1:
            raise FinamDownloadError('Duplicate items for id={} on market {}'
                                     .format(id_, market))
        code = items.iloc[0]['code']

        if end_date is None:
            end_date = datetime.date.today()

        params = {
            'p': period.value,
            'em': id_,
            'market': market.value,
            'df': start_date.day,
            'mf': start_date.month - 1,
            'yf': start_date.year,
            'dt': end_date.day,
            'mt': end_date.month - 1,
            'yt': end_date.year,
            'cn': code,
            'code': code,
            # I would guess this param denotes 'data format'
            # that differs for ticks only
            'datf': 6 if period == period.TICKS.value else 5
        }

        url = self.__build_url(params)
        # deliberatly not using pd.read_csv's ability to fetch
        # urls to fully control what's happening
        data = self._fetch(url)
        self.__do_sanity_checks(data)
        if period == Period.TICKS:
            date_cols = [2, 3]
        else:
            date_cols = [0, 1]

        try:
            df = pd.read_csv(StringIO(data),
                             index_col=0,
                             parse_dates={'index': date_cols},
                             sep=';')
            df.sort_index(inplace=True)
        except CParserError as e:
            raise FinamParsingError(e.message)

        return df