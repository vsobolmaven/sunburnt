


import collections, copy, operator, re
from six import string_types
from .schema import solr_date, SolrError, SolrBooleanField, SolrUnicodeField, WildcardFieldInstance
from .walktree import walk, event, leaf, exit
import six
from functools import reduce

class LuceneQuery(object):
    default_term_re = re.compile(r'^\w+$')
    def __init__(self, schema, option_flag=None, original=None):
        self.schema = schema
        self.normalized = False
        if original is None:
            self.option_flag = option_flag
            self.terms = collections.defaultdict(set)
            self.phrases = collections.defaultdict(set)
            self.ranges = set()
            self.subqueries = []
            self._and = True
            self._or = self._not = self._pow = False
            self.boosts = []
        else:
            self.option_flag = original.option_flag
            self.terms = copy.copy(original.terms)
            self.phrases = copy.copy(original.phrases)
            self.ranges = copy.copy(original.ranges)
            self.subqueries = copy.copy(original.subqueries)
            self._or = original._or
            self._and = original._and
            self._not = original._not
            self._pow = original._pow
            self.boosts = copy.copy(original.boosts)

    def clone(self, **kwargs):
        q = LuceneQuery(self.schema, original=self)
        for k, v in list(kwargs.items()):
            setattr(q, k, v)
        return q

    def options(self):
        opts = {}
        s = six.text_type(self)
        if s:
            opts[self.option_flag] = s
        return opts

    def serialize_debug(self, indent=0):
        indentspace = indent * ' '
        print('%s%s (%s)' % (indentspace, repr(self), "Normalized" if self.normalized else "Not normalized"))
        print('%s%s' % (indentspace, '{'))
        for term in list(self.terms.items()):
            print('%s%s' % (indentspace, term))
        for phrase in list(self.phrases.items()):
            print('%s%s' % (indentspace, phrase))
        for range in self.ranges:
            print('%s%s' % (indentspace, range))
        if self.subqueries:
            if self._and:
                print('%sAND:' % indentspace)
            elif self._or:
                print('%sOR:' % indentspace)
            elif self._not:
                print('%sNOT:' % indentspace)
            elif self._pow is not False:
                print('%sPOW %s:' % (indentspace, self._pow))
            else:
                raise ValueError
            for subquery in self.subqueries:
                subquery.serialize_debug(indent+2)
        print('%s%s' % (indentspace, '}'))

    # Below, we sort all our value_sets - this is for predictability when testing.
    def serialize_term_queries(self, terms):
        s = []
        for name, value_set in list(terms.items()):
            if name:
                field = self.schema.match_field(name)
            else:
                field = self.schema.default_field
            if name:
                s += ['%s:%s' % (name, value.to_query()) for value in value_set]
            else:
                s += [value.to_query() for value in value_set]
        return ' AND '.join(sorted(s))

    range_query_templates = {
        "any": "[* TO *]",
        "lt": "{* TO %s}",
        "lte": "[* TO %s]",
        "gt": "{%s TO *}",
        "gte": "[%s TO *]",
        "rangeexc": "{%s TO %s}",
        "range": "[%s TO %s]",
    }
    def serialize_range_queries(self):
        s = []
        for name, rel, values in sorted(self.ranges):
            range_s = self.range_query_templates[rel] % \
                tuple(value.to_query() for value in sorted(values, key=lambda x: getattr(x, "value")))
            s.append("%s:%s" % (name, range_s))
        return ' AND '.join(s)

    def child_needs_parens(self, child):
        if len(child) == 1:
            return False
        elif self._or:
            return not (child._or or child._pow)
        elif (self._and or self._not):
            return not (child._and or child._not or child._pow)
        elif self._pow is not False:
            return True
        else:
            return True

    def normalize(self):
        # shortcut to avoid re-normalization no-ops
        if self.normalized:
            return self, False

        changed = False
        for path in walk(self, lambda q: q.subqueries, event(exit|leaf)):
            if len(path) == 1:
                # last time around, so:
                break
            this = path[-1]
            obj = self.normalize_node(this)
            obj.normalized = True
            if obj != this:
                siblings = path[-2].subqueries
                i = siblings.index(this)
                siblings[i] = obj
                changed = True

        obj = self.normalize_node(self)
        return obj, (changed or obj == self)

    @staticmethod
    def merge_term_dicts(args):
        d = collections.defaultdict(set)
        for arg in args:
            for k, v in list(arg.items()):
                d[k].update(v)
        return dict((k, v) for k, v in list(d.items()))

    @staticmethod
    def normalize_node(obj):
        """ Normalize a query node provided all its sub-queries
        are already normalized"""
        # Recalculate terms/phrases/ranges/subqueries as appropriate given immediate subqueries
        terms = [obj.terms]
        phrases = [obj.phrases]
        ranges = [obj.ranges]
        subqueries = []

        mutated = False
        for s in obj.subqueries:
            if not s:
                mutated = True # we're dropping a subquery
                continue # don't append
            if (s._and and obj._and) or (s._or and obj._or):
                # then hoist the contents up
                terms.append(s.terms)
                phrases.append(s.phrases)
                ranges.append(s.ranges)
                subqueries.extend(s.subqueries)
                mutated = True
            else: # just keep it unchanged
                subqueries.append(s)

        # and clone if there have been any changes
        if mutated:
            obj = obj.clone(terms = obj.merge_term_dicts(terms),
                            phrases = obj.merge_term_dicts(phrases),
                            ranges = reduce(operator.or_, ranges),
                            subqueries = subqueries)

        # having recalculated subqueries, there may be the opportunity for further normalization, if we have zero or one subqueries left
        if not len(obj.subqueries):
            if obj._not:
                obj = obj.clone(_not=False, _and=True)
            elif obj._pow:
                obj = obj.clone(_pow=False)
        elif len(obj.subqueries) == 1:
            if obj._not and obj.subqueries[0]._not:
                obj = obj.clone(subqueries=obj.subqueries[0].subqueries, _not=False, _and=True)
            elif (obj._and or obj._or) and not obj.terms and not obj.phrases and not obj.ranges and not obj.boosts:
                obj = obj.subqueries[0]
        obj.normalized = True
        return obj

    def __str__(self):
        return self.serialize_to_unicode(level=0, op=None)

    def serialize_to_unicode(self, level=0, op=None):
        if not self.normalized:
            self, _ = self.normalize()
        if self.boosts:
            # Clone and rewrite to effect the boosts.
            newself = self.clone()
            newself.boosts = []
            boost_queries = [self.Q(**kwargs)**boost_score
                             for kwargs, boost_score in self.boosts]
            newself = newself | (newself & reduce(operator.or_, boost_queries))
            newself, _ = newself.normalize()
            return newself.serialize_to_unicode(level=level)
        else:
            u = [s for s in [self.serialize_term_queries(self.terms),
                             self.serialize_term_queries(self.phrases),
                             self.serialize_range_queries()]
                 if s]
            for q in self.subqueries:
                op_ = 'OR' if self._or else 'AND'
                if self.child_needs_parens(q):
                    u.append("(%s)"%q.serialize_to_unicode(level=level+1, op=op_))
                else:
                    u.append("%s"%q.serialize_to_unicode(level=level+1, op=op_))
            if self._and:
                return ' AND '.join(u)
            elif self._or:
                return ' OR '.join(u)
            elif self._not:
                assert len(u) == 1
                if level == 0 or (level == 1 and op == "AND"):
                    return 'NOT %s'%u[0]
                else:
                    return '(*:* AND NOT %s)'%u[0]
            elif self._pow is not False:
                assert len(u) == 1
                return "%s^%s"%(u[0], self._pow)
            else:
                raise ValueError

    def __len__(self):
        # How many terms in this (sub) query?
        if len(self.subqueries) == 1:
            subquery_length = len(self.subqueries[0])
        else:
            subquery_length = len(self.subqueries)
        return sum([sum(len(v) for v in list(self.terms.values())),
                    sum(len(v) for v in list(self.phrases.values())),
                    len(self.ranges),
                    subquery_length])

    def Q(self, *args, **kwargs):
        q = LuceneQuery(self.schema)
        q.add(args, kwargs)
        return q

    def __bool__(self):
        return bool(self.terms) or bool(self.phrases) or bool(self.ranges) or bool(self.subqueries)

    def __or__(self, other):
        q = LuceneQuery(self.schema)
        q._and = False
        q._or = True
        q.subqueries = [self, other]
        return q

    def __and__(self, other):
        q = LuceneQuery(self.schema)
        q.subqueries = [self, other]
        return q

    def __invert__(self):
        q = LuceneQuery(self.schema)
        q._and = False
        q._not = True
        q.subqueries = [self]
        return q

    def __pow__(self, value):
        try:
            float(value)
        except ValueError:
            raise ValueError("Non-numeric value supplied for boost")
        q = LuceneQuery(self.schema)
        q.subqueries = [self]
        q._and = False
        q._pow = value
        return q

    def add(self, args, kwargs):
        self.normalized = False
        _args = []
        for arg in args:
            if isinstance(arg, LuceneQuery):
                self.subqueries.append(arg)
            else:
                _args.append(arg)
        args = _args
        try:
            terms_or_phrases = kwargs.pop("__terms_or_phrases")
        except KeyError:
            terms_or_phrases = None
        for value in args:
            self.add_exact(None, value, terms_or_phrases)
        for k, v in list(kwargs.items()):
            try:
                field_name, rel = k.split("__")
            except ValueError:
                field_name, rel = k, 'eq'
            field = self.schema.match_field(field_name)
            if not field:
                if (k, v) != ("*", "*"):
                    # the only case where wildcards in field names are allowed
                    raise ValueError("%s is not a valid field name" % k)
            elif not field.indexed:
                raise SolrError("Can't query on non-indexed field '%s'" % field_name)
            if rel == 'eq':
                self.add_exact(field_name, v, terms_or_phrases)
            else:
                self.add_range(field_name, rel, v)

    def add_exact(self, field_name, values, term_or_phrase):
        # We let people pass in a list of values to match.
        # This really only makes sense for text fields or
        # multivalued fields.

        if any([not hasattr(values, "__iter__"), isinstance(values, string_types)]):
            values = [values]
        # We can only do a field_name == "*" if:
        if field_name and field_name != "*":
            field = self.schema.match_field(field_name)
        elif not field_name:
            field = self.schema.default_field
        else: # field_name must be "*"
            if len(values) == 1 and values[0] == "*":
                self.terms["*"].add(WildcardFieldInstance.from_user_data())
                return
            else:
                raise SolrError("If field_name is '*', then only '*' is permitted as the query")

        insts = [field.instance_from_user_data(value) for value in values]
        for inst in insts:
            if isinstance(field, SolrUnicodeField):
                this_term_or_phrase = term_or_phrase or self.term_or_phrase(inst.value)
            else:
                this_term_or_phrase = "terms"
            getattr(self, this_term_or_phrase)[field_name].add(inst)

    def add_range(self, field_name, rel, value):
        field = self.schema.match_field(field_name)
        if isinstance(field, SolrBooleanField):
            raise ValueError("Cannot do a '%s' query on a bool field" % rel)
        if rel not in self.range_query_templates:
            raise SolrError("No such relation '%s' defined" % rel)
        if rel in ('range', 'rangeexc'):
            try:
                assert len(value) == 2
            except (AssertionError, TypeError):
                raise SolrError("'%s__%s' argument must be a length-2 iterable"
                                 % (field_name, rel))
            insts = tuple(sorted(field.instance_from_user_data(v) for v in value))
        elif rel == 'any':
            if value is not True:
                raise SolrError("'%s__%s' argument must be True")
            insts = ()
        else:
            insts = (field.instance_from_user_data(value),)
        self.ranges.add((field_name, rel, insts))

    def term_or_phrase(self, arg, force=None):
        return 'terms' if self.default_term_re.match(arg) else 'phrases'

    def add_boost(self, kwargs, boost_score):
        for k, v in list(kwargs.items()):
            field = self.schema.match_field(k)
            if not field:
                raise ValueError("%s is not a valid field name" % k)
            elif not field.indexed:
                raise SolrError("Can't query on non-indexed field '%s'" % k)
            value = field.instance_from_user_data(v)
        self.boosts.append((kwargs, boost_score))



class BaseSearch(object):
    """Base class for common search options management"""
    option_modules = ('query_obj', 'filter_obj', 'paginator',
                      'more_like_this', 'highlighter', 'faceter',
                      'facet_ranger', 'sorter', 'facet_querier',
                      'field_limiter', 'extra')

    result_constructor = dict

    def _init_common_modules(self):
        self.query_obj = LuceneQuery(self.schema, 'q')
        self.filter_obj = FilterOptions(self.schema)
        self.paginator = PaginateOptions(self.schema)
        self.highlighter = HighlightOptions(self.schema)
        self.faceter = FacetOptions(self.schema)
        self.facet_ranger = FacetRangeOptions(self.schema)
        self.sorter = SortOptions(self.schema)
        self.field_limiter = FieldLimitOptions(self.schema)
        self.facet_querier = FacetQueryOptions(self.schema)
        self.extra = ExtraOptions(self.schema)

    def clone(self):
        return self.__class__(interface=self.interface, original=self)

    def Q(self, *args, **kwargs):
        q = LuceneQuery(self.schema)
        q.add(args, kwargs)
        return q

    def query(self, *args, **kwargs):
        newself = self.clone()
        newself.query_obj.add(args, kwargs)
        return newself

    def query_by_term(self, *args, **kwargs):
        return self.query(__terms_or_phrases="terms", *args, **kwargs)

    def query_by_phrase(self, *args, **kwargs):
        return self.query(__terms_or_phrases="phrases", *args, **kwargs)

    def exclude(self, *args, **kwargs):
        # cloning will be done by query
        return self.query(~self.Q(*args, **kwargs))

    def boost_relevancy(self, boost_score, **kwargs):
        if not self.query_obj:
            raise TypeError("Can't boost the relevancy of an empty query")
        try:
            float(boost_score)
        except ValueError:
            raise ValueError("Non-numeric boost value supplied")

        newself = self.clone()
        newself.query_obj.add_boost(kwargs, boost_score)
        return newself

    def filter(self, *args, **kwargs):
        newself = self.clone()
        newself.filter_obj.add(args, kwargs)
        return newself

    def filter_by_term(self, *args, **kwargs):
        return self.filter(__terms_or_phrases="terms", *args, **kwargs)

    def filter_by_phrase(self, *args, **kwargs):
        return self.filter(__terms_or_phrases="phrases", *args, **kwargs)

    def filter_exclude(self, *args, **kwargs):
        # cloning will be done by filter
        return self.filter(~self.Q(*args, **kwargs))

    def facet_by(self, field, **kwargs):
        newself = self.clone()
        newself.faceter.update(field, **kwargs)
        return newself

    def facet_by_range(self, field, **kwargs):
        newself = self.clone()
        newself.facet_ranger.update(field, **kwargs)
        return newself

    def facet_query(self, *args, **kwargs):
        newself = self.clone()
        newself.facet_querier.update(self.Q(*args, **kwargs))
        return newself

    def highlight(self, fields=None, **kwargs):
        newself = self.clone()
        newself.highlighter.update(fields, **kwargs)
        return newself

    def mlt(self, fields, query_fields=None, **kwargs):
        newself = self.clone()
        newself.more_like_this.update(fields, query_fields, **kwargs)
        return newself

    def paginate(self, start=None, rows=None):
        newself = self.clone()
        newself.paginator.update(start, rows)
        return newself

    def sort_by(self, field):
        newself = self.clone()
        newself.sorter.update(field)
        return newself

    def field_limit(self, fields=None, score=False, all_fields=False):
        newself = self.clone()
        newself.field_limiter.update(fields, score, all_fields)
        return newself

    def add_extra(self, **kwargs):
        newself = self.clone()
        newself.extra.update(kwargs)
        return newself

    def options(self):
        options = {}
        for option_module in self.option_modules:
            options.update(getattr(self, option_module).options())
        return dict((k, v) for k, v in list(options.items()))

    def results_as(self, constructor):
        newself = self.clone()
        newself.result_constructor = constructor
        return newself

    def transform_result(self, result, constructor):
        if constructor is not dict:
            construct_docs = lambda docs: [constructor(**d) for d in docs]
            result.result.docs = construct_docs(result.result.docs)
            for key in result.more_like_these:
                result.more_like_these[key].docs = \
                        construct_docs(result.more_like_these[key].docs)
            # in future, highlighting chould be made available to
            # custom constructors; perhaps document additional
            # arguments result constructors are required to support, or check for
            # an optional set_highlighting method
        else:
            if result.highlighting:
                for d in result.result.docs:
                    # if the unique key for a result doc is present in highlighting,
                    # add the highlighting for that document into the result dict
                    # (but don't override any existing content)
                    # If unique key field is not a string field (eg int) then we need to
                    # convert it to its solr representation
                    unique_key = self.schema.fields[self.schema.unique_key].to_solr(d[self.schema.unique_key])
                    if 'solr_highlights' not in d and \
                           unique_key in result.highlighting:
                        d['solr_highlights'] = result.highlighting[unique_key]
        return result

    def params(self):
        return params_from_dict(**self.options())

    ## methods to allow SolrSearch to be used with Django paginator ##

    _count = None
    def count(self):
        # get the total count for the current query without retrieving any results
        # cache it, since it may be needed multiple times when used with django paginator
        if self._count is None:
            # are we already paginated? then we'll behave as if that's
            # defined our result set already.
            if self.paginator.rows is not None:
                total_results = self.paginator.rows
            else:
                response = self.paginate(rows=0).execute()
                total_results = response.result.numFound
                if self.paginator.start is not None:
                    total_results -= self.paginator.start
            self._count = total_results
        return self._count

    __len__ = count

    def __getitem__(self, k):
        """Return a single result or slice of results from the query.
        """
        # are we already paginated? if so, we'll apply this getitem to the
        # paginated result - else we'll apply it to the whole.
        offset = 0 if self.paginator.start is None else self.paginator.start

        if isinstance(k, slice):
            # calculate solr pagination options for the requested slice
            step = operator.index(k.step) if k.step is not None else 1
            if step == 0:
                raise ValueError("slice step cannot be zero")
            if step > 0:
                s1 = k.start
                s2 = k.stop
                inc = 0
            else:
                s1 = k.stop
                s2 = k.start
                inc = 1

            if s1 is not None:
                start = operator.index(s1)
                if start < 0:
                    start += self.count()
                    start = max(0, start)
                start += inc
            else:
                start = 0
            if s2 is not None:
                stop = operator.index(s2)
                if stop < 0:
                    stop += self.count()
                    stop = max(0, stop)
                stop += inc
            else:
                stop = self.count()

            rows = stop - start
            if self.paginator.rows is not None:
                rows = min(rows, self.paginator.rows)
            rows = max(rows, 0)

            start += offset

            response = self.paginate(start=start, rows=rows).execute()
            if step != 1:
                response.result.docs = response.result.docs[::step]
            return response

        else:
            # if not a slice, a single result is being requested
            k = operator.index(k)
            if k < 0:
                k += self.count()
                if k < 0:
                    raise IndexError("list index out of range")

            # Otherwise do the query anyway, don't count() to avoid extra Solr call
            k += offset
            response = self.paginate(start=k, rows=1).execute()
            if response.result.numFound < k:
                raise IndexError("list index out of range")
            return response.result.docs[0]


class SolrSearch(BaseSearch):
    def __init__(self, interface, original=None):
        self.interface = interface
        self.schema = interface.schema
        if original is None:
            self.more_like_this = MoreLikeThisOptions(self.schema)
            self._init_common_modules()
        else:
            for opt in self.option_modules:
                setattr(self, opt, getattr(original, opt).clone())
            self.result_constructor = original.result_constructor

    def options(self):
        options = super(SolrSearch, self).options()
        if 'q' not in options:
            options['q'] = '*:*' # search everything
        return options

    def execute(self, constructor=None):
        if constructor is None:
            constructor = self.result_constructor
        result = self.interface.search(**self.options())
        return self.transform_result(result, constructor)


class MltSolrSearch(BaseSearch):
    """Manage parameters to build a MoreLikeThisHandler query"""
    trivial_encodings = ["utf_8", "u8", "utf", "utf8", "ascii", "646", "us_ascii"]
    def __init__(self, interface, content=None, content_charset=None, url=None,
                 original=None):
        self.interface = interface
        self.schema = interface.schema
        if original is None:
            if content is not None and url is not None:
                raise ValueError(
                    "Cannot specify both content and url")
            if content is not None:
                if content_charset is None:
                    content_charset = 'utf-8'
                if isinstance(content, six.text_type):
                    content = content.encode('utf-8')
                elif content_charset.lower().replace('-', '_') not in self.trivial_encodings:
                    content = content.decode(content_charset).encode('utf-8')
            self.content = content
            self.url = url
            self.more_like_this = MoreLikeThisHandlerOptions(self.schema)
            self._init_common_modules()
        else:
            self.content = original.content
            self.url = original.url
            for opt in self.option_modules:
                setattr(self, opt, getattr(original, opt).clone())

    def query(self, *args, **kwargs):
        if self.content is not None or self.url is not None:
            raise ValueError("Cannot specify query as well as content on an MltSolrSearch")
        return super(MltSolrSearch, self).query(*args, **kwargs)

    def query_by_term(self, *args, **kwargs):
        if self.content is not None or self.url is not None:
            raise ValueError("Cannot specify query as well as content on an MltSolrSearch")
        return super(MltSolrSearch, self).query_by_term(*args, **kwargs)

    def query_by_phrase(self, *args, **kwargs):
        if self.content is not None or self.url is not None:
            raise ValueError("Cannot specify query as well as content on an MltSolrSearch")
        return super(MltSolrSearch, self).query_by_phrase(*args, **kwargs)

    def exclude(self, *args, **kwargs):
        if self.content is not None or self.url is not None:
            raise ValueError("Cannot specify query as well as content on an MltSolrSearch")
        return super(MltSolrSearch, self).exclude(*args, **kwargs)

    def Q(self, *args, **kwargs):
        if self.content is not None or self.url is not None:
            raise ValueError("Cannot specify query as well as content on an MltSolrSearch")
        return super(MltSolrSearch, self).Q(*args, **kwargs)

    def boost_relevancy(self, *args, **kwargs):
        if self.content is not None or self.url is not None:
            raise ValueError("Cannot specify query as well as content on an MltSolrSearch")
        return super(MltSolrSearch, self).boost_relevancy(*args, **kwargs)

    def options(self):
        options = super(MltSolrSearch, self).options()
        if self.url is not None:
            options['stream.url'] = self.url
        return options

    def execute(self, constructor=dict):
        result = self.interface.mlt_search(content=self.content, **self.options())
        return self.transform_result(result, constructor)


class Options(object):
    def clone(self):
        return self.__class__(self.schema, self)

    def invalid_value(self, msg=""):
        assert False, msg

    def update(self, fields=None, **kwargs):
        if fields:
            self.schema.check_fields(fields)
            if isinstance(fields, six.string_types):
                fields = [fields]
            for field in set(fields) - set(self.fields):
                self.fields[field] = {}
        elif kwargs:
            fields = [None]
        checked_kwargs = self.check_opts(kwargs)
        for k, v in list(checked_kwargs.items()):
            for field in fields:
                self.fields[field][k] = v

    def check_opts(self, kwargs):
        checked_kwargs = {}
        for k, v in list(kwargs.items()):
            if k not in self.opts:
                raise SolrError("No such option for %s: %s" % (self.option_name, k))
            opt_type = self.opts[k]
            try:
                if isinstance(opt_type, (list, tuple)):
                    assert v in opt_type
                elif isinstance(opt_type, type):
                    v = opt_type(v)
                else:
                    v = opt_type(self, v)
            except:
                raise SolrError("Invalid value for %s option %s: %s" % (self.option_name, k, v))
            checked_kwargs[k] = v
        return checked_kwargs

    def options(self):
        opts = {}
        if self.fields:
            opts[self.option_name] = True
            fields = [field for field in self.fields if field]
            self.field_names_in_opts(opts, fields)
        for field_name, field_opts in list(self.fields.items()):
            if not field_name:
                for field_opt, v in list(field_opts.items()):
                    opts['%s.%s'%(self.option_name, field_opt)] = v
            else:
                for field_opt, v in list(field_opts.items()):
                    opts['f.%s.%s.%s'%(field_name, self.option_name, field_opt)] = v
        return opts


class FilterOptions(object):
    """
    This class creates a list of filters, so that we end up with multiple
    fq arguments to Solr.
    """
    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.filters = []
        else:
            self.filters = [copy.copy(f) for f in original.filters]

    def clone(self):
        return self.__class__(self.schema, self)

    def add(self, *args, **kwargs):
        fq_filter = LuceneQuery(self.schema)
        fq_filter.add(*args, **kwargs)
        self.filters.append(fq_filter)

    def options(self):
        if self.filters:
            return {'fq': [f.options()[None] for f in self.filters]}
        else:
            return {}


class FacetOptions(Options):
    option_name = "facet"
    opts = {"prefix":six.text_type,
            "sort":[True, False, "count", "index"],
            "limit":int,
            "offset":lambda self, x: int(x) >= 0 and int(x) or self.invalid_value(),
            "mincount":lambda self, x: int(x) >= 0 and int(x) or self.invalid_value(),
            "missing":bool,
            "method":["enum", "fc"],
            "enum.cache.minDf":int,
            }

    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.fields = collections.defaultdict(dict)
        else:
            self.fields = copy.copy(original.fields)

    def field_names_in_opts(self, opts, fields):
        if fields:
            opts["facet.field"] = sorted(fields)

class FacetRangeOptions(Options):
    option_name = "facet.range"
    opts = {"end": lambda self, v: self.__validate_range_endpoint(v),
            "gap": lambda self, v: self.__validate_range_gap(v),
            "hardend": bool,
            "include": ["lower", "upper", "edge", "outer", "all"],
            "other": ["before", "after", "between", "none", "all"],
            "start": lambda self, v: self.__validate_range_endpoint(v),
            }

    # The list of valid Lucene unit keywords isn't documented anywhere except in its source code:
    # http://svn.apache.org/repos/asf/lucene/dev/trunk/solr/core/src/java/org/apache/solr/util/DateMathParser.java
    # Scroll to makeUnitsMap().
    lucene_units = ["YEAR", "YEARS", "MONTH", "MONTHS", "DAY", "DAYS", "DATE",
                    "HOUR", "HOURS", "MINUTE", "MINUTES", "SECOND", "SECONDS",
                    "MILLI", "MILLIS", "MILLISECOND", "MILLISECONDS",]
    lucene_unit_pattern = re.compile(r'^([-+])(\d+)(\w+)$')

    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.fields = collections.defaultdict(dict)
        else:
            self.fields = copy.copy(original.fields)

    def __validate_range_endpoint(self, v):
        """
        Validate that the argument is a valid endpoint for a Solr range facet.

        This includes integers, floats, and special strings like "+1YEAR".
        """
        if isinstance(v, (int, float)):
            return v
        elif isinstance(v, solr_date):
            return six.text_type(v)
        else:
            return self.invalid_value()

    def __validate_range_gap(self, v):
        if isinstance(v, (int, float)):
            return v
        elif isinstance(v, six.string_types):
            # A string gap must use Lucene syntax:
            # http://lucene.apache.org/solr/4_0_0/solr-core/org/apache/solr/util/DateMathParser.html
            match = self.__class__.lucene_unit_pattern.match(v)

            if match is None:
                return self.invalid_value()
            elif match.group(3).upper() not in self.__class__.lucene_units:
                return self.invalid_value()

            return v
        else:
            return self.invalid_value()

    def update(self, fields=None, **kwargs):
        assert isinstance(fields, dict)
        self.fields = dict()
        if fields:
            self.schema.check_fields(list(fields.keys()))
            for field, opts in list(fields.items()):
                self.fields[field] = dict()
                checked_kwargs = self.check_opts(dict(list(opts.items()) + list(kwargs.items())))
                for k, v in list(checked_kwargs.items()):
                    self.fields[field][k] = v

            # Validate field options.
            if not ("start" in opts and "end" in opts and "gap" in opts):
                raise SolrError("Start, end, and gap are required for range facet on '%s'." % field)

            if (opts["start"] > opts["end"]):
                raise SolrError("Range start for '%s' cannot be greater than range end." % field)

            if isinstance(opts["start"], int) and not (isinstance(opts["end"], int) and isinstance(opts["gap"], int)):
                raise SolrError("Incompatible types for start, end, and gap on '%s'." % field)

            if isinstance(opts["start"], float) and not (isinstance(opts["end"], float) and isinstance(opts["gap"], float)):
                raise SolrError("Incompatible types for start, end, and gap on '%s'." % field)

            if isinstance(opts["start"], solr_date) and not (isinstance(opts["end"], solr_date) and isinstance(opts["gap"], six.string_types)):
                raise SolrError("Incompatible types for start, end, and gap on '%s'." % field)

    def options(self):
        opts = {}

        if self.fields:
            opts["facet"] = True
            opts[self.option_name] = True
            fields = [field for field in list(self.fields.keys()) if field]
            self.field_names_in_opts(opts, fields)
            for field_name, field_opts in list(self.fields.items()):
                if field_name:
                    for field_opt, v in list(field_opts.items()):
                        opts['f.%s.%s.%s'%(field_name, self.option_name, field_opt)] = v

        return opts

    def field_names_in_opts(self, opts, fields):
        if fields:
            opts["facet.range"] = fields

        return opts

class HighlightOptions(Options):
    option_name = "hl"
    opts = {"snippets":int,
            "fragsize":int,
            "mergeContinuous":bool,
            "requireFieldMatch":bool,
            "maxAnalyzedChars":int,
            "alternateField":lambda self, x: x if x in self.schema.fields else self.invalid_value(),
            "maxAlternateFieldLength":int,
            "formatter":["simple"],
            "simple.pre":six.text_type,
            "simple.post":six.text_type,
            "fragmenter":six.text_type,
            "useFastVectorHighlighter":bool,	# available as of Solr 3.1
            "usePhraseHighlighter":bool,
            "highlightMultiTerm":bool,
            "regex.slop":float,
            "regex.pattern":six.text_type,
            "regex.maxAnalyzedChars":int
            }
    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.fields = collections.defaultdict(dict)
        else:
            self.fields = copy.copy(original.fields)

    def field_names_in_opts(self, opts, fields):
        if fields:
            opts["hl.fl"] = ",".join(sorted(fields))


class MoreLikeThisOptions(Options):
    option_name = "mlt"
    opts = {"count":int,
            "mintf":int,
            "mindf":int,
            "minwl":int,
            "maxwl":int,
            "maxqt":int,
            "maxntp":int,
            "boost":bool,
            }
    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.fields = set()
            self.query_fields = {}
            self.kwargs = {}
        else:
            self.fields = copy.copy(original.fields)
            self.query_fields = copy.copy(original.query_fields)
            self.kwargs = copy.copy(original.kwargs)

    def update(self, fields, query_fields=None, **kwargs):
        if fields is None:
            fields = [self.schema.default_field_name]
        self.schema.check_fields(fields)
        if isinstance(fields, six.string_types):
            fields = [fields]
        self.fields.update(fields)

        if query_fields is not None:
            for k, v in list(query_fields.items()):
                if k not in self.fields:
                    raise SolrError("'%s' specified in query_fields but not fields"% k)
                if v is not None:
                    try:
                        v = float(v)
                    except ValueError:
                        raise SolrError("'%s' has non-numerical boost value"% k)
            self.query_fields.update(query_fields)

        checked_kwargs = self.check_opts(kwargs)
        self.kwargs.update(checked_kwargs)

    def options(self):
        opts = {}
        if self.fields:
            opts['mlt'] = True
            opts['mlt.fl'] = ','.join(sorted(self.fields))

        if self.query_fields:
            qf_arg = []
            for k, v in list(self.query_fields.items()):
                if v is None:
                    qf_arg.append(k)
                else:
                    qf_arg.append("%s^%s" % (k, float(v)))
            opts["mlt.qf"] = " ".join(qf_arg)

        for opt_name, opt_value in list(self.kwargs.items()):
            opt_type = self.opts[opt_name]
            opts["mlt.%s" % opt_name] = opt_type(opt_value)

        return opts


class MoreLikeThisHandlerOptions(MoreLikeThisOptions):
    opts = {'match.include': bool,
            'match.offset': int,
            'interestingTerms': ["list", "details", "none"],
           }
    opts.update(MoreLikeThisOptions.opts)
    del opts['count']

    def options(self):
        opts = {}
        if self.fields:
            opts['mlt.fl'] = ','.join(sorted(self.fields))

        if self.query_fields:
            qf_arg = []
            for k, v in list(self.query_fields.items()):
                if v is None:
                    qf_arg.append(k)
                else:
                    qf_arg.append("%s^%s" % (k, float(v)))
            opts["mlt.qf"] = " ".join(qf_arg)

        for opt_name, opt_value in list(self.kwargs.items()):
            opts["mlt.%s" % opt_name] = opt_value

        return opts


class PaginateOptions(Options):
    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.start = None
            self.rows = None
        else:
            self.start = original.start
            self.rows = original.rows

    def update(self, start, rows):
        if start is not None:
            if start < 0:
                raise SolrError("paginator start index must be 0 or greater")
            self.start = start
        if rows is not None:
            if rows < 0:
                raise SolrError("paginator rows must be 0 or greater")
            self.rows = rows

    def options(self):
        opts = {}
        if self.start is not None:
            opts['start'] = self.start
        if self.rows is not None:
            opts['rows'] = self.rows
        return opts


class SortOptions(Options):
    option_name = "sort"
    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.fields = []
        else:
            self.fields = copy.copy(original.fields)

    def update(self, field):
        # We're not allowing function queries a la Solr1.5
        if field.startswith('-'):
            order = "desc"
            field = field[1:]
        elif field.startswith('+'):
            order = "asc"
            field = field[1:]
        else:
            order = "asc"
        if field != 'score':
            f = self.schema.match_field(field)
            if not f:
                raise SolrError("No such field %s" % field)
            elif f.multi_valued:
                raise SolrError("Cannot sort on a multivalued field")
            elif not f.indexed:
                raise SolrError("Cannot sort on an un-indexed field")
        self.fields.append([order, field])

    def options(self):
        if self.fields:
            return {"sort":", ".join("%s %s" % (field, order) for order, field in self.fields)}
        else:
            return {}


class FieldLimitOptions(Options):
    option_name = "fl"

    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.fields = set()
            self.score = False
            self.all_fields = False
        else:
            self.fields = copy.copy(original.fields)
            self.score = original.score
            self.all_fields = original.all_fields

    def update(self, fields=None, score=False, all_fields=False):
        if fields is None:
            fields = []
        if isinstance(fields, six.string_types):
            fields = [fields]
        self.schema.check_fields(fields, {"stored": True})
        self.fields.update(fields)
        self.score = score
        self.all_fields = all_fields

    def options(self):
        opts = {}
        if self.all_fields:
            fields = set("*")
        else:
            fields = self.fields
        if self.score:
            fields.add("score")
        if fields:
            opts['fl'] = ','.join(sorted(fields))
        return opts


class FacetQueryOptions(Options):
    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.queries = []
        else:
            self.queries = [q.clone() for q in original.queries]

    def update(self, query):
        self.queries.append(query)

    def options(self):
        if self.queries:
            return {'facet.query':[six.text_type(q) for q in self.queries],
                    'facet':True}
        else:
            return {}


class ExtraOptions(Options):
    def __init__(self, schema, original=None):
        self.schema = schema
        if original is None:
            self.option_dict = {}
        else:
            self.option_dict = original.option_dict.copy()

    def update(self, extra_options):
        self.option_dict.update(extra_options)

    def options(self):
        return self.option_dict


def params_from_dict(**kwargs):
    utf8_params = []

    for k, vs in list(kwargs.items()):
        # We allow for multivalued options with lists.
        if any([not hasattr(vs, "__iter__"), isinstance(vs, string_types)]):
            vs = [vs]
        for v in vs:
            if isinstance(v, bool):
                v = "true" if v else "false"
            else:
                v = six.text_type(v)
            utf8_params.append((k, v))
    return sorted(utf8_params)
