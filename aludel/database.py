import json

from alchimia import TWISTED_STRATEGY
from sqlalchemy import MetaData, Table, Column, String, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable
from twisted.internet.defer import succeed


def get_engine(conn_str, reactor):
    return create_engine(conn_str, reactor=reactor, strategy=TWISTED_STRATEGY)


class TableMissingError(Exception):
    pass


class CollectionMissingError(Exception):
    pass


class make_table(object):
    def __init__(self, *args, **kw):
        self.args = args
        self.kw = kw

    def make_table(self, name, metadata):
        return Table(name, metadata, *self.copy_args(), **self.kw)

    def copy_args(self):
        for arg in self.args:
            if isinstance(arg, Column):
                yield arg.copy()
            else:
                yield arg


class _PrefixedTables(object):
    def __init__(self, name, connection):
        self.name = name
        self._conn = connection
        self._metadata = MetaData()
        for attr in dir(self):
            attrval = getattr(self, attr)
            if isinstance(attrval, make_table):
                setattr(self, attr, attrval.make_table(
                    self.get_table_name(attr), self._metadata))

    def get_table_name(self, name):
        raise NotImplementedError(
            "_PrefixedTables should not be used directly.")

    def _create_table(self, trx, table):
        # This works around alchimia's current inability to create tables only
        # if they don't already exist.

        table_exists_err_templates = [
            'table %(name)s already exists',
            'table "%(name)s" already exists',
        ]

        def table_exists_errback(f):
            f.trap(OperationalError)
            for err_template in table_exists_err_templates:
                if err_template % {'name': table.name} in str(f.value):
                    return None
            return f

        d = self._conn.execute(CreateTable(table))
        d.addErrback(table_exists_errback)
        return d.addCallback(lambda r: trx)

    def _create_tables(self):
        d = self._conn.begin()
        for table in self._metadata.sorted_tables:
            d.addCallback(self._create_table, table)
        return d.addCallback(lambda trx: trx.commit())

    def exists(self):
        raise NotImplementedError(
            "_PrefixedTables should not be used directly.")

    def execute_query(self, query, *args, **kw):
        def table_missing_errback(f):
            f.trap(OperationalError)
            if 'no such table: ' in str(f.value):
                raise TableMissingError(f.value.args[0])
            return f

        d = self._conn.execute(query, *args, **kw)
        return d.addErrback(table_missing_errback)

    def execute_fetchall(self, query, *args, **kw):
        d = self.execute_query(query, *args, **kw)
        return d.addCallback(lambda result: result.fetchall())


class CollectionMetadata(_PrefixedTables):
    """
    Metadata manager for PrefixedTableCollection.

    This tracks table prefixes and metadata for a given collection type.
    """

    collection_metadata = make_table(
        Column("name", String(), primary_key=True),
        Column("metadata_json", String(), nullable=False),
    )

    _metadata_cache_dict = None

    @property
    def _metadata_cache(self):
        if self._metadata_cache_dict is None:
            self._metadata_cache_dict = {}
        return self._metadata_cache_dict

    def get_table_name(self, name):
        return '%s_%s' % (name, self.name)

    def exists(self):
        # It would be nice to make this not use private things.
        return self._conn._engine.has_table(self.collection_metadata.name)

    def create(self):
        return self._create_tables()

    def _update_metadata(self, new_metadata, clear=False):
        if clear:
            self._metadata_cache.clear()
        self._metadata_cache.update(new_metadata)

    def _add_row_to_metadata(self, row, name):
        metadata = None
        if row is not None:
            metadata = json.loads(row.metadata_json)
        self._update_metadata({name: metadata})
        return metadata

    def _raise_if_none(self, metadata, name):
        if metadata is None:
            raise CollectionMissingError(name)
        return metadata

    def get_metadata(self, name):
        cache = self._metadata_cache
        if name not in cache:
            d = self.execute_query(
                self.collection_metadata.select().where(
                    self.collection_metadata.c.name == name))
            d.addCallback(lambda result: result.fetchone())
            d.addCallback(self._add_row_to_metadata, name)
        else:
            d = succeed(cache[name])
        d.addCallback(self._raise_if_none, name)
        return d

    def set_metadata(self, name, metadata):
        self._metadata_cache.pop(name, None)
        d = self.execute_query(
            self.collection_metadata.update().where(
                self.collection_metadata.c.name == name,
            ).values(metadata_json=json.dumps(metadata)))
        d.addCallback(lambda result: {name: metadata})
        d.addCallback(self._update_metadata)
        return d

    def _create_collection(self, exists, name, metadata):
        if exists:
            return
        d = self.execute_query(self.collection_metadata.insert().values(
            name=name, metadata_json=json.dumps(metadata)))
        d.addCallback(lambda result: {name: metadata})
        d.addCallback(self._update_metadata)
        return d

    def create_collection(self, name, metadata=None):
        if metadata is None:
            metadata = {}
        d = self.collection_exists(name)
        d.addCallback(self._create_collection, name, metadata)
        return d

    def collection_exists(self, name):
        d = self.get_metadata(name)

        def missing_to_false_eb(f):
            f.trap(CollectionMissingError)
            return False

        d.addCallbacks(lambda _: True, missing_to_false_eb)
        return d


class TableCollection(_PrefixedTables):
    """
    Collection of database tables sharing a common prefix.

    Each table is prefixed with the collection type and name.

    The collection type defaults to the class name, but the
    :attr:`COLLECTION_TYPE` class attribute may be set to override this.
    """

    COLLECTION_TYPE = None

    def __init__(self, name, connection, collection_metadata=None):
        super(TableCollection, self).__init__(name, connection)
        if collection_metadata is None:
            collection_metadata = CollectionMetadata(
                self.collection_type(), connection)
        self._collection_metadata = collection_metadata

    @classmethod
    def collection_type(cls):
        ctype = cls.COLLECTION_TYPE
        if ctype is None:
            ctype = cls.__name__
        return ctype

    def get_table_name(self, name):
        return '%s_%s_%s' % (self.collection_type(), self.name, name)

    def exists(self):
        return self._collection_metadata.collection_exists(self.name)

    def create_tables(self, metadata=None):
        d = self._create_tables()
        d.addCallback(lambda _: self._collection_metadata.create_collection(
            self.name, metadata))
        return d

    def get_metadata(self):
        return self._collection_metadata.get_metadata(self.name)

    def set_metadata(self, metadata):
        return self._collection_metadata.set_metadata(self.name, metadata)
