"""
This file is concerned with providing a simple interface for data stored in
Elasticsearch.  The class(es) defined here are fed into the preprocessing step.
"""

from abc import ABCMeta, abstractmethod, abstractproperty
import logging
import time

from elasticsearch import Elasticsearch, helpers
from six import with_metaclass

from topik.intermediaries.persistence import Persistor


def _get_hash_identifier(input_data, id_field):
    return hash(input_data[id_field])


class CorpusInterface(with_metaclass(ABCMeta)):
    def __init__(self):
        super(CorpusInterface, self).__init__()
        self.persistor = Persistor()

    @classmethod
    @abstractmethod
    def class_key(cls):
        """Implement this method to return the string ID with which to store your class."""
        raise NotImplementedError

    @abstractmethod
    def __iter__(self):
        """This is expected to iterate over your data, returning tuples of (doc_id, <selected field>)"""
        raise NotImplementedError

    @abstractmethod
    def __len__(self):
        raise NotImplementedError

    @abstractmethod
    def get_generator_without_id(self, field=None):
        """Returns a generator that yields field content without doc_id associate"""
        raise NotImplementedError

    @abstractmethod
    def append_to_record(self, record_id, field_name, field_value):
        """Used to store preprocessed output alongside input data.

        Field name is destination.  Value is processed value."""
        raise NotImplementedError

    @abstractmethod
    def get_date_filtered_data(self, start, end, field):
        raise NotImplementedError

    @abstractproperty
    def filter_string(self):
        raise NotImplementedError

    def save(self, filename, saved_data=None):
        """Persist this object to disk somehow.

        You can save your data in any number of files in any format, but at a minimum, you need one json file that
        describes enough to bootstrap the loading prcess.  Namely, you must have a key called 'class' so that upon
        loading the output, the correct class can be instantiated and used to load any other data.  You don't have
        to implement anything for saved_data, but it is stored as a key next to 'class'.

        """
        self.persistor.store_corpus({"class": self.__class__.class_key(), "saved_data": saved_data})
        self.persistor.persist_data(filename)


class ElasticSearchCorpus(CorpusInterface):
    def __init__(self, host, index, content_field, port=9200, username=None,
                 password=None, doc_type=None, query=None, iterable=None,
                 filter_expression=""):
        super(ElasticSearchCorpus, self).__init__()
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.instance = Elasticsearch(hosts=[{"host": host, "port": port,
                                              "http_auth": "{}:{}".format(username, password)}
                                             ])
        self.index = index
        self.content_field = content_field
        self.doc_type = doc_type
        self.query = query
        if iterable:
            self.import_from_iterable(iterable, content_field)
        self.filter_expression = filter_expression

    @classmethod
    def class_key(cls):
        return "elastic"

    @property
    def filter_string(self):
        return self.filter_expression

    def __iter__(self):
        results = helpers.scan(self.instance, index=self.index,
                               query=self.query, doc_type=self.doc_type)
        for result in results:
            yield result["_id"], result['_source'][self.content_field]

    def __len__(self):
        return self.instance.count(index=self.index, doc_type=self.doc_type)["count"]

    def get_generator_without_id(self, field=None):
        if not field:
            field = self.content_field
        results = helpers.scan(self.instance, index=self.index,
                               query=self.query, doc_type=self.doc_type)
        for result in results:
            yield result["_source"][field]

    def append_to_record(self, record_id, field_name, field_value):
        self.instance.update(index=self.index, id=record_id, doc_type="continuum",
                             body={"doc": {field_name: field_value}})

    def get_field(self, field=None):
        """Get a different field to iterate over, keeping all other
           connection details."""
        if not field:
            field = self.content_field
        return ElasticSearchCorpus(self.host, self.index, field, self.port,
                                   self.username, self.password, self.doc_type,
                                   self.query)

    def import_from_iterable(self, iterable, id_field="text", batch_size=500):
        """Load data into Elasticsearch from iterable.

        iterable: generally a list of dicts, but possibly a list of strings
            This is your data.  Your dictionary structure defines the schema
            of the elasticsearch index.
        id_field: string identifier of field to hash for content ID.  For
            list of dicts, a valid key value in the dictionary is required. For
            list of strings, a dictionary with one key, "text" is created and
            used.
        """
        batch = []
        for item in iterable:
            if isinstance(item, basestring):
                item = {id_field: item}
            id = _get_hash_identifier(item, id_field)
            batch.append({"_id": id, "_source": item, "_type": "continuum"})
            if len(batch) >= batch_size:
                helpers.bulk(client=self.instance, actions=batch, index=self.index)
                batch = []
        if batch:
            helpers.bulk(client=self.instance, actions=batch, index=self.index)

    def convert_date_field_and_reindex(self, field):
        index = self.index
        if self.instance.indices.get_field_mapping(field=field,
                                           index=index,
                                           doc_type="continuum") != 'date':
            index = self.index+"_{}_date".format(field)
            if not self.instance.indices.exists(index) or self.instance.indices.get_field_mapping(field=field,
                                           index=index,
                                           doc_type="continuum") != 'date':
                mapping = self.instance.indices.get_mapping(index=self.index,
                                                            doc_type="continuum")
                mapping[self.index]["mappings"]["continuum"]["properties"][field] = {"type": "date"}
                self.instance.indices.put_alias(index=self.index,
                                                name=index,
                                                body=mapping)
                while self.instance.count(index=self.index) != self.instance.count(index=index):
                    logging.info("Waiting for date indexed data to be indexed...")
                    time.sleep(1)
        return index

    # TODO: validate input data to ensure that it has valid year data
    def get_date_filtered_data(self, start, end, field="date"):
        converted_index = self.convert_date_field_and_reindex(field=field)
        return ElasticSearchCorpus(self.host, converted_index, self.content_field, self.port,
                                   self.username, self.password, self.doc_type,
                                   query={"query":
                                          {"range":
                                           {field:
                                            {"gte": start,
                                             "lte": end}}}},
                                   filter_expression=self.filter_expression + "_date_{}_{}".format(start, end))

    def save(self, filename, saved_data=None):
        if saved_data is None:
            saved_data = {"host": self.host, "port": self.port, "index": self.index,
                          "content_field": self.content_field, "username": self.username,
                          "password": self.password, "doc_type": self.doc_type, "query": self.query}
        return super(ElasticSearchCorpus, self).save(filename, saved_data)


class DictionaryCorpus(CorpusInterface):
    def __init__(self, content_field, iterable=None, generate_id=True, reference_field=None, content_filter=None):
        super(DictionaryCorpus, self).__init__()
        self.content_field = content_field
        self._documents = []
        self.idx = 0
        active_field = None
        if reference_field:
            self.reference_field = reference_field
            active_field = content_field
            content_field = reference_field
        else:
            self.reference_field = content_field
        if iterable:
            self.import_from_iterable(iterable, content_field, generate_id)
        if active_field:
            self.content_field = active_field
        self.content_filter = content_filter

    @classmethod
    def class_key(cls):
        return "dictionary"

    def __iter__(self):
        for doc in self._documents:
            if self.content_filter:
                if eval(self.content_filter["expression"].format(doc["_source"][self.content_filter["field"]])):
                    yield doc["_id"], doc["_source"][self.content_field]
            else:
                yield doc["_id"], doc["_source"][self.content_field]

    def __len__(self):
        return len(self._documents)

    @property
    def filter_string(self):
        return self.content_filter["expression"].format(self.content_filter["field"]) if self.content_filter else ""

    def append_to_record(self, record_id, field_name, field_value):
        for doc in self._documents:
            if doc["_id"] == record_id:
                doc["_source"][field_name] = field_value
                return
        raise ValueError("No record with id '{}' was found.".format(record_id))

    def get_field(self, field=None):
        """Get a different field to iterate over, keeping all other details."""
        if not field:
            field = self.content_field
        return DictionaryCorpus(content_field=field, iterable=self._documents,
                                generate_id=False, reference_field=self.content_field)

    def get_generator_without_id(self, field=None):
        if not field:
            field = self.content_field
        for doc in self._documents:
            yield doc["_source"][field]

    def import_from_iterable(self, iterable, content_field, generate_id=True):
        """
        iterable: generally a list of dicts, but possibly a list of strings
            This is your data.  Your dictionary structure defines the schema
            of the elasticsearch index.
        """
        if generate_id:
            self._documents = [{"_id": hash(doc[content_field]),
                                "_source": doc} for doc in iterable]
            self.reference_field = content_field
        else:
            self._documents = [item for item in iterable]

    # TODO: generalize for datetimes
    # TODO: validate input data to ensure that it has valid year data
    def get_date_filtered_data(self, start, end, field="year"):
        return DictionaryCorpus(content_field=field, iterable=self._documents,
                                generate_id=False, reference_field=self.content_field,
                                content_filter={"field": field, "expression": "{}<=int({})<={}".format(start, "{}", end)})

    def save(self, filename, saved_data=None):
        if saved_data is None:
            saved_data = {"reference_field": self.reference_field, "content_field": self.content_field,
                          "iterable": [doc["_source"] for doc in self._documents]}
        return super(DictionaryCorpus, self).save(filename, saved_data)

# Collection of output formats: people put files, folders, etc in, and they can choose from these to be the output
# These consume the iterable collection of dictionaries produced by the various iter_ functions.
output_formats = {cls.class_key(): cls for cls in CorpusInterface.__subclasses__()}


def load_persisted_corpus(filename):
    corpus_dict = Persistor(filename).get_corpus_dict()
    return output_formats[corpus_dict['class']](**corpus_dict["saved_data"])
