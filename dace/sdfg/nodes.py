""" Contains classes implementing the different types of nodes of the stateful
    dataflow multigraph representation. """

import ast
from copy import deepcopy as dcpy
import dace
import itertools
import dace.serialize
from typing import Any, Dict, Set
from dace.config import Config
from dace.sdfg import graph
from dace.frontend.python.astutils import unparse
from dace.properties import (Property, CodeProperty, LambdaProperty,
                             RangeProperty, DebugInfoProperty, SetProperty,
                             make_properties, indirect_properties,
                             DataProperty, SymbolicProperty, ListProperty,
                             SDFGReferenceProperty, DictProperty,
                             LibraryImplementationProperty, CodeBlock)
from dace.frontend.operations import detect_reduction_type
from dace import data, subsets as sbs, dtypes
import pydoc
import warnings

# -----------------------------------------------------------------------------


@make_properties
class Node(object):
    """ Base node class. """

    in_connectors = SetProperty(
        str, default=set(), desc="A set of input connectors for this node.")
    out_connectors = SetProperty(
        str, default=set(), desc="A set of output connectors for this node.")

    def __init__(self, in_connectors=None, out_connectors=None):
        self.in_connectors = in_connectors or set()
        self.out_connectors = out_connectors or set()

    def __str__(self):
        if hasattr(self, 'label'):
            return self.label
        else:
            return type(self).__name__

    def validate(self, sdfg, state):
        pass

    def to_json(self, parent):
        labelstr = str(self)
        typestr = getattr(self, '__jsontype__', str(type(self).__name__))

        try:
            scope_entry_node = parent.entry_node(self)
        except (RuntimeError, StopIteration):
            scope_entry_node = None

        if scope_entry_node is not None:
            ens = parent.exit_node(parent.entry_node(self))
            scope_exit_node = str(parent.node_id(ens))
            scope_entry_node = str(parent.node_id(scope_entry_node))
        else:
            scope_entry_node = None
            scope_exit_node = None

        # The scope exit of an entry node is the matching exit node
        if isinstance(self, EntryNode):
            try:
                scope_exit_node = str(parent.node_id(parent.exit_node(self)))
            except (RuntimeError, StopIteration):
                scope_exit_node = None

        retdict = {
            "type": typestr,
            "label": labelstr,
            "attributes": dace.serialize.all_properties_to_json(self),
            "id": parent.node_id(self),
            "scope_entry": scope_entry_node,
            "scope_exit": scope_exit_node
        }
        return retdict

    def __repr__(self):
        return type(self).__name__ + ' (' + self.__str__() + ')'

    def add_in_connector(self, connector_name: str):
        """ Adds a new input connector to the node. The operation will fail if
            a connector (either input or output) with the same name already
            exists in the node.

            :param connector_name: The name of the new connector.
            :return: True if the operation is successful, otherwise False.
        """

        if (connector_name in self.in_connectors
                or connector_name in self.out_connectors):
            return False
        connectors = self.in_connectors
        connectors.add(connector_name)
        self.in_connectors = connectors
        return True

    def add_out_connector(self, connector_name: str):
        """ Adds a new output connector to the node. The operation will fail if
            a connector (either input or output) with the same name already
            exists in the node.

            :param connector_name: The name of the new connector.
            :return: True if the operation is successful, otherwise False.
        """

        if (connector_name in self.in_connectors
                or connector_name in self.out_connectors):
            return False
        connectors = self.out_connectors
        connectors.add(connector_name)
        self.out_connectors = connectors
        return True

    def remove_in_connector(self, connector_name: str):
        """ Removes an input connector from the node.
            :param connector_name: The name of the connector to remove.
            :return: True if the operation was successful.
        """

        if connector_name in self.in_connectors:
            connectors = self.in_connectors
            connectors.remove(connector_name)
            self.in_connectors = connectors
        return True

    def remove_out_connector(self, connector_name: str):
        """ Removes an output connector from the node.
            :param connector_name: The name of the connector to remove.
            :return: True if the operation was successful.
        """

        if connector_name in self.out_connectors:
            connectors = self.out_connectors
            connectors.remove(connector_name)
            self.out_connectors = connectors
        return True

    def _next_connector_int(self) -> int:
        """ Returns the next unused connector ID (as an integer). Used for
            filling connectors when adding edges to scopes. """
        next_number = 1
        for conn in itertools.chain(self.in_connectors, self.out_connectors):
            if conn.startswith('IN_'):
                cconn = conn[3:]
            elif conn.startswith('OUT_'):
                cconn = conn[4:]
            else:
                continue
            try:
                curconn = int(cconn)
                if curconn >= next_number:
                    next_number = curconn + 1
            except (TypeError, ValueError):  # not integral
                continue
        return next_number

    def next_connector(self) -> str:
        """ Returns the next unused connector ID (as a string). Used for
            filling connectors when adding edges to scopes. """
        return str(self._next_connector_int())

    def last_connector(self) -> str:
        """ Returns the last used connector ID (as a string). Used for
            filling connectors when adding edges to scopes. """
        return str(self._next_connector_int() - 1)

    @property
    def free_symbols(self) -> Set[str]:
        """ Returns a set of symbols used in this node's properties. """
        return set()

    def new_symbols(self, sdfg, state, symbols) -> Dict[str, dtypes.typeclass]:
        """ Returns a mapping between symbols defined by this node (e.g., for
            scope entries) to their type. """
        return {}


# ------------------------------------------------------------------------------


@make_properties
class AccessNode(Node):
    """ A node that accesses data in the SDFG. Denoted by a circular shape. """

    access = Property(choices=dtypes.AccessType,
                      desc="Type of access to this array",
                      default=dtypes.AccessType.ReadWrite)
    setzero = Property(dtype=bool, desc="Initialize to zero", default=False)
    debuginfo = DebugInfoProperty()
    data = DataProperty(desc="Data (array, stream, scalar) to access")

    def __init__(self,
                 data,
                 access=dtypes.AccessType.ReadWrite,
                 debuginfo=None):
        super(AccessNode, self).__init__()

        # Properties
        self.debuginfo = debuginfo
        self.access = access
        if not isinstance(data, str):
            raise TypeError('Data for AccessNode must be a string')
        self.data = data

    @staticmethod
    def from_json(json_obj, context=None):
        ret = AccessNode("Nodata")
        dace.serialize.set_properties_from_json(ret, json_obj, context=context)
        return ret

    def __deepcopy__(self, memo):
        node = object.__new__(AccessNode)
        node._access = self._access
        node._data = self._data
        node._setzero = self._setzero
        node._in_connectors = self._in_connectors
        node._out_connectors = self._out_connectors
        node.debuginfo = dcpy(self.debuginfo)
        return node

    @property
    def label(self):
        return self.data

    def __label__(self, sdfg, state):
        return self.data

    def desc(self, sdfg):
        from dace.sdfg import SDFGState, ScopeSubgraphView
        if isinstance(sdfg, (SDFGState, ScopeSubgraphView)):
            sdfg = sdfg.parent
        return sdfg.arrays[self.data]

    def validate(self, sdfg, state):
        if self.data not in sdfg.arrays:
            raise KeyError('Array "%s" not found in SDFG' % self.data)


# ------------------------------------------------------------------------------


@make_properties
class CodeNode(Node):
    """ A node that contains runnable code with acyclic external data
        dependencies. May either be a tasklet or a nested SDFG, and
        denoted by an octagonal shape. """

    label = Property(dtype=str, desc="Name of the CodeNode")
    location = DictProperty(
        key_type=str,
        value_type=dace.symbolic.pystr_to_symbolic,
        desc='Full storage location identifier (e.g., rank, GPU ID)')
    environments = SetProperty(
        str,
        desc="Environments required by CMake to build and run this code node.",
        default=set())

    def __init__(self, label="", location=None, inputs=None, outputs=None):
        super(CodeNode, self).__init__(inputs or set(), outputs or set())
        # Properties
        self.label = label
        self.location = location if location is not None else {}

    @property
    def free_symbols(self) -> Set[str]:
        return set().union(*[v.free_symbols for v in self.location.values()])


@make_properties
class Tasklet(CodeNode):
    """ A node that contains a tasklet: a functional computation procedure
        that can only access external data specified using connectors.

        Tasklets may be implemented in Python, C++, or any supported
        language by the code generator.
    """

    code = CodeProperty(desc="Tasklet code", default=CodeBlock(""))
    debuginfo = DebugInfoProperty()

    instrument = Property(
        choices=dtypes.InstrumentationType,
        desc="Measure execution statistics with given method",
        default=dtypes.InstrumentationType.No_Instrumentation)

    def __init__(self,
                 label,
                 inputs=None,
                 outputs=None,
                 code="",
                 language=dtypes.Language.Python,
                 location=None,
                 debuginfo=None):
        super(Tasklet, self).__init__(label, location, inputs, outputs)

        self.code = CodeBlock(code, language)
        self.debuginfo = debuginfo

    @property
    def language(self):
        return self.code.language

    @staticmethod
    def from_json(json_obj, context=None):
        ret = Tasklet("dummylabel")
        dace.serialize.set_properties_from_json(ret, json_obj, context=context)
        return ret

    @property
    def name(self):
        return self._label

    def validate(self, sdfg, state):
        if not dtypes.validate_name(self.label):
            raise NameError('Invalid tasklet name "%s"' % self.label)
        for in_conn in self.in_connectors:
            if not dtypes.validate_name(in_conn):
                raise NameError('Invalid input connector "%s"' % in_conn)
        for out_conn in self.out_connectors:
            if not dtypes.validate_name(out_conn):
                raise NameError('Invalid output connector "%s"' % out_conn)

    def __str__(self):
        if not self.label:
            return "--Empty--"
        else:
            return self.label


# ------------------------------------------------------------------------------


@make_properties
class NestedSDFG(CodeNode):
    """ An SDFG state node that contains an SDFG of its own, runnable using
        the data dependencies specified using its connectors.

        It is encouraged to use nested SDFGs instead of coarse-grained tasklets
        since they are analyzable with respect to transformations.

        @note: A nested SDFG cannot create recursion (one of its parent SDFGs).
    """

    # NOTE: We cannot use SDFG as the type because of an import loop
    sdfg = SDFGReferenceProperty(desc="The SDFG", allow_none=True)
    schedule = Property(dtype=dtypes.ScheduleType,
                        desc="SDFG schedule",
                        allow_none=True,
                        choices=dtypes.ScheduleType,
                        from_string=lambda x: dtypes.ScheduleType[x],
                        default=dtypes.ScheduleType.Default)
    symbol_mapping = DictProperty(
        key_type=str,
        value_type=dace.symbolic.pystr_to_symbolic,
        desc="Mapping between internal symbols and their values, expressed as "
        "symbolic expressions")
    debuginfo = DebugInfoProperty()
    is_collapsed = Property(dtype=bool,
                            desc="Show this node/scope/state as collapsed",
                            default=False)

    instrument = Property(
        choices=dtypes.InstrumentationType,
        desc="Measure execution statistics with given method",
        default=dtypes.InstrumentationType.No_Instrumentation)

    def __init__(self,
                 label,
                 sdfg,
                 inputs: Set[str],
                 outputs: Set[str],
                 symbol_mapping: Dict[str, Any] = None,
                 schedule=dtypes.ScheduleType.Default,
                 location=None,
                 debuginfo=None):
        super(NestedSDFG, self).__init__(label, location, inputs, outputs)

        # Properties
        self.sdfg = sdfg
        self.symbol_mapping = symbol_mapping or {}
        self.schedule = schedule
        self.debuginfo = debuginfo

    @staticmethod
    def from_json(json_obj, context=None):
        from dace import SDFG  # Avoid import loop

        # We have to load the SDFG first.
        ret = NestedSDFG("nolabel", SDFG('nosdfg'), set(), set())

        dace.serialize.set_properties_from_json(ret, json_obj, context)

        if context and 'sdfg_state' in context:
            ret.sdfg.parent = context['sdfg_state']
        if context and 'sdfg' in context:
            ret.sdfg.parent_sdfg = context['sdfg']

        ret.sdfg.update_sdfg_list([])

        return ret

    @property
    def free_symbols(self) -> Set[str]:
        return set().union(
            *(v.free_symbols for v in self.symbol_mapping.values()),
            *(v.free_symbols for v in self.location.values()))

    def __str__(self):
        if not self.label:
            return "SDFG"
        else:
            return self.label

    def validate(self, sdfg, state):
        if not dtypes.validate_name(self.label):
            raise NameError('Invalid nested SDFG name "%s"' % self.label)
        for in_conn in self.in_connectors:
            if not dtypes.validate_name(in_conn):
                raise NameError('Invalid input connector "%s"' % in_conn)
        for out_conn in self.out_connectors:
            if not dtypes.validate_name(out_conn):
                raise NameError('Invalid output connector "%s"' % out_conn)
        connectors = self.in_connectors | self.out_connectors
        for dname, desc in self.sdfg.arrays.items():
            # TODO(later): Disallow scalars without access nodes (so that this
            #              check passes for them too).
            if isinstance(desc, data.Scalar):
                continue
            if not desc.transient and dname not in connectors:
                raise NameError('Data descriptor "%s" not found in nested '
                                'SDFG connectors' % dname)
            if dname in connectors and desc.transient:
                raise NameError(
                    '"%s" is a connector but its corresponding array is transient'
                    % dname)

        # Validate undefined symbols
        symbols = set(k for k in self.sdfg.symbols.keys()
                      if k not in connectors)
        missing_symbols = [s for s in symbols if s not in self.symbol_mapping]
        if missing_symbols:
            raise ValueError('Missing symbols on nested SDFG: %s' %
                             (missing_symbols))

        # Recursively validate nested SDFG
        self.sdfg.validate()


# ------------------------------------------------------------------------------


# Scope entry class
class EntryNode(Node):
    """ A type of node that opens a scope (e.g., Map or Consume). """
    def validate(self, sdfg, state):
        self.map.validate(sdfg, state, self)


# ------------------------------------------------------------------------------


# Scope exit class
class ExitNode(Node):
    """ A type of node that closes a scope (e.g., Map or Consume). """
    def validate(self, sdfg, state):
        self.map.validate(sdfg, state, self)


# ------------------------------------------------------------------------------


@dace.serialize.serializable
class MapEntry(EntryNode):
    """ Node that opens a Map scope.
        @see: Map
    """
    def __init__(self, map: 'Map', dynamic_inputs=None):
        super(MapEntry, self).__init__(dynamic_inputs or set())
        if map is None:
            raise ValueError("Map for MapEntry can not be None.")
        self._map = map

    @staticmethod
    def map_type():
        return Map

    @classmethod
    def from_json(cls, json_obj, context=None):
        m = cls.map_type()("", [], [])
        ret = cls(map=m)

        try:
            # Connection of the scope nodes
            try:
                nid = int(json_obj['scope_exit'])
            except KeyError:
                # Backwards compatibility
                nid = int(json_obj['scope_exits'][0])

            exit_node = context['sdfg_state'].node(nid)
            exit_node.map = m
        except IndexError:  # Exit node has a higher node ID
            # Connection of the scope nodes handled in MapExit
            pass

        dace.serialize.set_properties_from_json(ret, json_obj, context=context)
        return ret

    @property
    def map(self):
        return self._map

    @map.setter
    def map(self, val):
        self._map = val

    def __str__(self):
        return str(self.map)

    @property
    def free_symbols(self) -> Set[str]:
        dyn_inputs = set(c for c in self.in_connectors
                         if not c.startswith('IN_'))
        return set(k for k in self._map.range.free_symbols
                   if k not in dyn_inputs)

    def new_symbols(self, sdfg, state, symbols) -> Dict[str, dtypes.typeclass]:
        from dace.codegen.tools.type_inference import infer_expr_type

        result = {}
        # Add map params
        for p, rng in zip(self._map.params, self._map.range):
            result[p] = dtypes.result_type_of(infer_expr_type(rng[0], symbols),
                                              infer_expr_type(rng[1], symbols))

        # Add dynamic inputs
        dyn_inputs = set(c for c in self.in_connectors
                         if not c.startswith('IN_'))

        # TODO: Get connector type from connector
        for e in state.in_edges(self):
            if e.dst_conn in dyn_inputs:
                result[e.dst_conn] = sdfg.arrays[e.data.data].dtype

        return result


@dace.serialize.serializable
class MapExit(ExitNode):
    """ Node that closes a Map scope.
        @see: Map
    """
    def __init__(self, map: 'Map'):
        super(MapExit, self).__init__()
        if map is None:
            raise ValueError("Map for MapExit can not be None.")
        self._map = map

    @staticmethod
    def map_type():
        return Map

    @classmethod
    def from_json(cls, json_obj, context=None):
        try:
            # Set map reference to map entry
            entry_node = context['sdfg_state'].node(
                int(json_obj['scope_entry']))

            ret = cls(map=entry_node.map)
        except IndexError:  # Entry node has a higher ID than exit node
            # Connection of the scope nodes handled in MapEntry
            ret = cls(cls.map_type()('_', [], []))

        dace.serialize.set_properties_from_json(ret, json_obj, context=context)

        return ret

    @property
    def map(self):
        return self._map

    @map.setter
    def map(self, val):
        self._map = val

    @property
    def schedule(self):
        return self._map.schedule

    @schedule.setter
    def schedule(self, val):
        self._map.schedule = val

    @property
    def label(self):
        return self._map.label

    def __str__(self):
        return str(self.map)


@make_properties
class Map(object):
    """ A Map is a two-node representation of parametric graphs, containing
        an integer set by which the contents (nodes dominated by an entry
        node and post-dominated by an exit node) are replicated.

        Maps contain a `schedule` property, which specifies how the scope
        should be scheduled (execution order). Code generators can use the
        schedule property to generate appropriate code, e.g., GPU kernels.
    """

    # List of (editable) properties
    label = Property(dtype=str, desc="Label of the map")
    params = ListProperty(element_type=str, desc="Mapped parameters")
    range = RangeProperty(desc="Ranges of map parameters",
                          default=sbs.Range([]))
    schedule = Property(dtype=dtypes.ScheduleType,
                        desc="Map schedule",
                        choices=dtypes.ScheduleType,
                        from_string=lambda x: dtypes.ScheduleType[x],
                        default=dtypes.ScheduleType.Default)
    unroll = Property(dtype=bool, desc="Map unrolling")
    collapse = Property(dtype=int,
                        default=1,
                        desc="How many dimensions to"
                        " collapse into the parallel range")
    debuginfo = DebugInfoProperty()
    is_collapsed = Property(dtype=bool,
                            desc="Show this node/scope/state as collapsed",
                            default=False)

    instrument = Property(
        choices=dtypes.InstrumentationType,
        desc="Measure execution statistics with given method",
        default=dtypes.InstrumentationType.No_Instrumentation)

    def __init__(self,
                 label,
                 params,
                 ndrange,
                 schedule=dtypes.ScheduleType.Default,
                 unroll=False,
                 collapse=1,
                 fence_instrumentation=False,
                 debuginfo=None):
        super(Map, self).__init__()

        # Assign properties
        self.label = label
        self.schedule = schedule
        self.unroll = unroll
        self.collapse = 1
        self.params = params
        self.range = ndrange
        self.debuginfo = debuginfo
        self._fence_instrumentation = fence_instrumentation

    def __str__(self):
        return self.label + "[" + ", ".join([
            "{}={}".format(i, r)
            for i, r in zip(self._params,
                            [sbs.Range.dim_to_string(d) for d in self._range])
        ]) + "]"

    def validate(self, sdfg, state, node):
        if not dtypes.validate_name(self.label):
            raise NameError('Invalid map name "%s"' % self.label)

    def get_param_num(self):
        """ Returns the number of map dimension parameters/symbols. """
        return len(self.params)


# Indirect Map properties to MapEntry and MapExit
MapEntry = indirect_properties(Map, lambda obj: obj.map)(MapEntry)

# ------------------------------------------------------------------------------


@dace.serialize.serializable
class ConsumeEntry(EntryNode):
    """ Node that opens a Consume scope.
        @see: Consume
    """
    def __init__(self, consume, dynamic_inputs=None):
        super(ConsumeEntry, self).__init__(dynamic_inputs or set())
        if consume is None:
            raise ValueError("Consume for ConsumeEntry can not be None.")
        self._consume = consume
        self.add_in_connector('IN_stream')
        self.add_out_connector('OUT_stream')

    @staticmethod
    def from_json(json_obj, context=None):
        c = Consume("", ['i', 1], None)
        ret = ConsumeEntry(consume=c)

        try:
            # Set map reference to map exit
            try:
                nid = int(json_obj['scope_exit'])
            except KeyError:
                # Backwards compatibility
                nid = int(json_obj['scope_exits'][0])

            exit_node = context['sdfg_state'].node(nid)
            exit_node.consume = c
        except IndexError:  # Exit node has a higher node ID
            # Connection of the scope nodes handled in ConsumeExit
            pass

        dace.serialize.set_properties_from_json(ret, json_obj, context=context)
        return ret

    @property
    def map(self):
        return self._consume.as_map()

    @property
    def consume(self):
        return self._consume

    @consume.setter
    def consume(self, val):
        self._consume = val

    def __str__(self):
        return str(self.consume)

    def free_symbols(self) -> Set[str]:
        dyn_inputs = set(c for c in self.in_connectors
                         if not c.startswith('IN_'))
        return ((set(self._consume.num_pes.free_symbols)
                 | set(self._consume.condition.free_symbols)) - dyn_inputs)

    def new_symbols(self, sdfg, state, symbols) -> Dict[str, dtypes.typeclass]:
        from dace.codegen.tools.type_inference import infer_expr_type

        result = {}
        # Add PE index
        result[self._consume.pe_index] = infer_expr_type(
            self._consume.num_pes, symbols)

        # Add dynamic inputs
        dyn_inputs = set(c for c in self.in_connectors
                         if not c.startswith('IN_'))

        # TODO: Get connector type from connector
        for e in state.in_edges(self):
            if e.dst_conn in dyn_inputs:
                result[e.dst_conn] = sdfg.arrays[e.data.data].dtype

        return result


@dace.serialize.serializable
class ConsumeExit(ExitNode):
    """ Node that closes a Consume scope.
        @see: Consume
    """
    def __init__(self, consume):
        super(ConsumeExit, self).__init__()
        if consume is None:
            raise ValueError("Consume for ConsumeExit can not be None.")
        self._consume = consume

    @staticmethod
    def from_json(json_obj, context=None):
        try:
            # Set consume reference to entry node
            entry_node = context['sdfg_state'].node(
                int(json_obj['scope_entry']))
            ret = ConsumeExit(consume=entry_node.consume)
        except IndexError:  # Entry node has a higher ID than exit node
            # Connection of the scope nodes handled in ConsumeEntry
            ret = ConsumeExit(Consume("", ['i', 1], None))

        dace.serialize.set_properties_from_json(ret, json_obj, context=context)
        return ret

    @property
    def map(self):
        return self._consume.as_map()

    @property
    def consume(self):
        return self._consume

    @consume.setter
    def consume(self, val):
        self._consume = val

    @property
    def schedule(self):
        return self._consume.schedule

    @schedule.setter
    def schedule(self, val):
        self._consume.schedule = val

    @property
    def label(self):
        return self._consume.label

    def __str__(self):
        return str(self.consume)


@make_properties
class Consume(object):
    """ Consume is a scope, like `Map`, that is a part of the parametric
        graph extension of the SDFG. It creates a producer-consumer
        relationship between the input stream and the scope subgraph. The
        subgraph is scheduled to a given number of processing elements
        for processing, and they will try to pop elements from the input
        stream until a given quiescence condition is reached. """

    # Properties
    label = Property(dtype=str, desc="Name of the consume node")
    pe_index = Property(dtype=str, desc="Processing element identifier")
    num_pes = SymbolicProperty(desc="Number of processing elements", default=1)
    condition = CodeProperty(desc="Quiescence condition", allow_none=True)
    schedule = Property(dtype=dtypes.ScheduleType,
                        desc="Consume schedule",
                        choices=dtypes.ScheduleType,
                        from_string=lambda x: dtypes.ScheduleType[x],
                        default=dtypes.ScheduleType.Default)
    chunksize = Property(dtype=int,
                         desc="Maximal size of elements to consume at a time",
                         default=1)
    debuginfo = DebugInfoProperty()
    is_collapsed = Property(dtype=bool,
                            desc="Show this node/scope/state as collapsed",
                            default=False)

    instrument = Property(
        choices=dtypes.InstrumentationType,
        desc="Measure execution statistics with given method",
        default=dtypes.InstrumentationType.No_Instrumentation)

    def as_map(self):
        """ Compatibility function that allows to view the consume as a map,
            mainly in memlet propagation. """
        return Map(self.label, [self.pe_index],
                   sbs.Range([(0, self.num_pes - 1, 1)]), self.schedule)

    def __init__(self,
                 label,
                 pe_tuple,
                 condition,
                 schedule=dtypes.ScheduleType.Default,
                 chunksize=1,
                 debuginfo=None):
        super(Consume, self).__init__()

        # Properties
        self.label = label
        self.pe_index, self.num_pes = pe_tuple
        self.condition = condition
        self.schedule = schedule
        self.chunksize = chunksize
        self.debuginfo = debuginfo

    def __str__(self):
        if self.condition is not None:
            return ("%s [%s=0:%s], Condition: %s" %
                    (self._label, self.pe_index, self.num_pes,
                     CodeProperty.to_string(self.condition)))
        else:
            return ("%s [%s=0:%s]" %
                    (self._label, self.pe_index, self.num_pes))

    def validate(self, sdfg, state, node):
        if not dtypes.validate_name(self.label):
            raise NameError('Invalid consume name "%s"' % self.label)

    def get_param_num(self):
        """ Returns the number of consume dimension parameters/symbols. """
        return 1


# Redirect Consume properties to ConsumeEntry and ConsumeExit
ConsumeEntry = indirect_properties(Consume,
                                   lambda obj: obj.consume)(ConsumeEntry)

# ------------------------------------------------------------------------------


@dace.serialize.serializable
class PipelineEntry(MapEntry):
    @staticmethod
    def map_type():
        return Pipeline

    @property
    def pipeline(self):
        return self._map

    @pipeline.setter
    def pipeline(self, val):
        self._map = val


@dace.serialize.serializable
class PipelineExit(MapExit):
    @staticmethod
    def map_type():
        return Pipeline

    @property
    def pipeline(self):
        return self._map

    @pipeline.setter
    def pipeline(self, val):
        self._map = val


@make_properties
class Pipeline(Map):
    """ This a convenience-subclass of Map that allows easier implementation of
        loop nests (using regular Map indices) that need a constant-sized
        initialization and drain phase (e.g., N*M + c iterations), which would
        otherwise need a flattened one-dimensional map.
    """
    init_size = Property(dtype=int,
                         desc="Number of initialization iterations.")
    init_overlap = Property(
        dtype=int,
        desc="Whether to increment regular map indices during initialization.")
    drain_size = Property(dtype=int, desc="Number of drain iterations.")
    drain_overlap = Property(
        dtype=int,
        desc="Whether to increment regular map indices during pipeline drain.")

    def __init__(self,
                 *args,
                 init_size=0,
                 init_overlap=False,
                 drain_size=0,
                 drain_overlap=False,
                 **kwargs):
        super(Pipeline, self).__init__(*args, **kwargs)
        self.init_size = init_size
        self.init_overlap = init_overlap
        self.drain_size = drain_size
        self.drain_overlap = drain_overlap
        self.flatten = True

    def iterator_str(self):
        return "__" + "".join(self.params)

    def loop_bound_str(self):
        from dace.codegen.targets.common import sym2cpp
        bound = 1
        for begin, end, step in self.range:
            bound *= (step + end - begin) // step
        # Add init and drain phases when relevant
        add_str = (" + " + sym2cpp(self.init_size)
                   if self.init_size != 0 and not self.init_overlap else "")
        add_str += (" + " + sym2cpp(self.drain_size)
                    if self.drain_size != 0 and not self.drain_overlap else "")
        return sym2cpp(bound) + add_str

    def init_condition(self):
        """Variable that can be checked to see if pipeline is currently in
           initialization phase."""
        if self.init_size <= 0:
            raise ValueError("No init condition exists for " + self.label)
        return self.iterator_str() + "_init"

    def drain_condition(self):
        """Variable that can be checked to see if pipeline is currently in
           draining phase."""
        if self.drain_size <= 0:
            raise ValueError("No drain condition exists for " + self.label)
        return self.iterator_str() + "_drain"


PipelineEntry = indirect_properties(Pipeline,
                                    lambda obj: obj.map)(PipelineEntry)

# ------------------------------------------------------------------------------


@make_properties
class LibraryNode(CodeNode):

    name = Property(dtype=str, desc="Name of node")
    implementation = LibraryImplementationProperty(
        dtype=str,
        allow_none=True,
        desc=("Which implementation this library node will expand into."
              "Must match a key in the list of possible implementations."))
    schedule = Property(
        dtype=dtypes.ScheduleType,
        desc="If set, determines the default device mapping of "
        "the node upon expansion, if expanded to a nested SDFG.",
        choices=dtypes.ScheduleType,
        from_string=lambda x: dtypes.ScheduleType[x],
        default=dtypes.ScheduleType.Default)

    def __init__(self, name, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = name
        self.label = name

    # Overrides subclasses to return LibraryNode as their JSON type
    @property
    def __jsontype__(self):
        return 'LibraryNode'

    # Based on https://stackoverflow.com/a/2020083/6489142
    def _fullclassname(self):
        module = self.__class__.__module__
        if module is None or module == str.__class__.__module__:
            return self.__class__.__name__  # Avoid reporting __builtin__
        else:
            return module + '.' + self.__class__.__name__

    def to_json(self, parent):
        jsonobj = super().to_json(parent)
        jsonobj['classpath'] = self._fullclassname()
        return jsonobj

    @classmethod
    def from_json(cls, json_obj, context=None):
        if cls == LibraryNode:
            clazz = pydoc.locate(json_obj['classpath'])
            if clazz is None:
                raise TypeError('Unrecognized library node type "%s"' %
                                json_obj['classpath'])
            return clazz.from_json(json_obj, context)
        else:  # Subclasses are actual library nodes
            ret = cls(json_obj['attributes']['name'])
            dace.serialize.set_properties_from_json(ret,
                                                    json_obj,
                                                    context=context)
            return ret

    def expand(self, sdfg, state, *args, **kwargs):
        """Create and perform the expansion transformation for this library
           node."""
        implementation = self.implementation
        library_name = type(self)._dace_library_name
        try:
            config_implementation = Config.get("library", library_name,
                                               "default_implementation")
        except KeyError:
            # Non-standard libraries are not defined in the config schema, and
            # thus might not exist in the config.
            config_implementation = None
        if config_implementation is not None:
            try:
                config_override = Config.get("library", library_name,
                                             "override")
                if config_override:
                    if implementation is not None:
                        warnings.warn(
                            "Overriding explicitly specified "
                            "implementation {} for {} with {}.".format(
                                implementation, self.label,
                                config_implementation))
                    implementation = config_implementation
            except KeyError:
                config_override = False
        # If not explicitly set, try the node default
        if implementation is None:
            implementation = type(self).default_implementation
            # If no node default, try library default
            if implementation is None:
                import dace.library  # Avoid cyclic dependency
                lib = dace.library._DACE_REGISTERED_LIBRARIES[type(
                    self)._dace_library_name]
                implementation = lib.default_implementation
                # Try the default specified in the config
                if implementation is None:
                    implementation = config_implementation
                    # Otherwise we don't know how to expand
                    if implementation is None:
                        raise ValueError("No implementation or default "
                                         "implementation specified.")
        if implementation not in self.implementations.keys():
            raise KeyError("Unknown implementation: " + implementation)
        transformation_type = type(self).implementations[implementation]
        sdfg_id = sdfg.sdfg_list.index(sdfg)
        state_id = sdfg.nodes().index(state)
        subgraph = {transformation_type._match_node: state.node_id(self)}
        transformation = transformation_type(sdfg_id, state_id, subgraph, 0)
        transformation.apply(sdfg, *args, **kwargs)

    @classmethod
    def register_implementation(cls, name, transformation_type):
        """Register an implementation to belong to this library node type."""
        cls.implementations[name] = transformation_type
        match_node_name = "__" + transformation_type.__name__
        if (hasattr(transformation_type, "_match_node")
                and transformation_type._match_node != match_node_name):
            raise ValueError(
                "Transformation " + transformation_type.__name__ +
                " is already registered with a different library node.")
        transformation_type._match_node = cls(match_node_name)
