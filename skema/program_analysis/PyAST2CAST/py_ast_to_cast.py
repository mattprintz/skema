import ast
from enum import unique
import os
import copy
import sys
from functools import singledispatchmethod

from skema.utils.misc import uuid
from skema.program_analysis.astpp import parseprint
from skema.program_analysis.CAST2GrFN.model.cast import (
    AstNode,
    Assignment,
    Attribute,
    BinaryOp,
    BinaryOperator,
    Boolean,
    Call,
    Dict,
    Expr,
    FunctionDef,
    List,
    LiteralValue,
    Loop,
    ModelBreak,
    ModelContinue,
    ModelIf,
    ModelReturn,
    ModelImport,
    Module,
    Name,
    Number,
    RecordDef,
    ScalarType,
    Set,
    String,
    StructureType,
    SourceRef,
    SourceCodeDataType,
    Subscript,
    Tuple,
    UnaryOp,
    UnaryOperator,
    VarType,
    Var,
    ValueConstructor,
    source_code_data_type,
    source_ref,
)
from skema.program_analysis.PyAST2CAST.modules_list import (
    BUILTINS,
    find_std_lib_module,
)


def merge_dicts(prev_scope, curr_scope):
    """merge_dicts
    Helper function to isolate the work of merging two dictionaries by merging
    key : value pairs from prev_scope into curr_scope
    The merging is done 'in_place'. That is, after the function is done, curr_scope
    is updated with any new key : value pairs that weren't in there before.

    Args:
        prev_scope (dict): Dictionary of name : ID pairs for variables in the enclosing scope
        curr_scope (dict): Dictionary of name : ID pairs for variables in the current scope
    """
    for k in prev_scope.keys():
        if k not in curr_scope.keys():
            curr_scope[k] = prev_scope[k]


def construct_unique_name(attr_name, var_name):
    """Constructs strings in the form of
    "attribute.var"
    where 'attribute' is either
        - the name of a module
        - an object

    Returns:
        string: A string representing a unique name

    """
    return f"{attr_name}.{var_name}"


def get_node_name(ast_node):
    if isinstance(ast_node, ast.Assign):
        return [ast_node[0].id]
    elif isinstance(ast_node, ast.Attribute):
        return [""]
    elif isinstance(ast_node, Attribute):
        return [ast_node.attr.name]
    elif isinstance(ast_node, Var):
        return [ast_node.val.name]
    elif isinstance(ast_node, Assignment):
        if isinstance(ast_node.left, Subscript):
            return [ast_node.left.value.name]
        else:
            return get_node_name(ast_node.left)
    elif isinstance(ast_node, Tuple):
        names = []
        for e in ast_node.values:
            names.extend(get_node_name(e))
        return names
    elif (
        isinstance(ast_node, LiteralValue)
        and ast_node.value_type == StructureType.LIST
    ):
        names = []
        for e in ast_node.value:
            names.extend(get_node_name(e))
        return names
    elif isinstance(ast_node, Subscript):
        raise TypeError(f"Type {ast_node} not supported")
    else:
        raise TypeError(f"Type {type(ast_node)} not supported")


def get_op(operator):
    ops = {
        ast.Add: BinaryOperator.ADD,
        ast.Sub: BinaryOperator.SUB,
        ast.Mult: BinaryOperator.MULT,
        ast.Div: BinaryOperator.DIV,
        ast.FloorDiv: BinaryOperator.FLOORDIV,
        ast.Mod: BinaryOperator.MOD,
        ast.Pow: BinaryOperator.POW,
        ast.LShift: BinaryOperator.LSHIFT,
        ast.RShift: BinaryOperator.RSHIFT,
        ast.BitOr: BinaryOperator.BITOR,
        ast.BitAnd: BinaryOperator.BITAND,
        ast.BitXor: BinaryOperator.BITXOR,
        ast.And: BinaryOperator.AND,
        ast.Or: BinaryOperator.OR,
        ast.Eq: BinaryOperator.EQ,
        ast.NotEq: BinaryOperator.NOTEQ,
        ast.Lt: BinaryOperator.LT,
        ast.LtE: BinaryOperator.LTE,
        ast.Gt: BinaryOperator.GT,
        ast.GtE: BinaryOperator.GTE,
        ast.In: BinaryOperator.IN,
        ast.NotIn: BinaryOperator.NOTIN,
        ast.UAdd: UnaryOperator.UADD,
        ast.USub: UnaryOperator.USUB,
        ast.Not: UnaryOperator.NOT,
        ast.Invert: UnaryOperator.INVERT,
    }

    if type(operator) in ops.keys():
        return ops[type(operator)]
    return None


class PyASTToCAST:
    """Class PyASTToCast
    This class is used to convert a Python program into a CAST object.
    In particular, given a PyAST object that represents the Python program's
    Abstract Syntax Tree, we create a Common Abstract Syntax Tree
    representation of it.
    Most of the functions involve visiting the children
    to generate their CAST, and then connecting them to their parent to form
    the parent node's CAST representation.
    The functions, in most cases, return lists containing their generated CAST.
    This is because in many scenarios (like in slicing) we need to return multiple
    values at once, since multiple CAST nodes gt generated. Returning lists allows us to
    do this, and as long as the visitors handle the data correctly, the CAST will be
    properly generated.
    All the visitors retrieve line number information from the PyAST nodes, and
    include the information in their respective CAST nodes, with the exception
    of the Module CAST visitor.

    This class inherits from ast.NodeVisitor, to allow us to use the Visitor
    design pattern to visit all the different kinds of PyAST nodes in a
    similar fashion.

    Current Fields:
        - Aliases
        - Visited
        - Filenames
        - Classes
        - Var_Count
        - global_identifier_dict
    """

    def __init__(self, file_name: str, legacy: Boolean = False):
        """Initializes any auxiliary data structures that are used
        for generating CAST.
        The current data structures are:
        - aliases: A dictionary used to keep track of aliases that imports use
                  (like import x as y, or from x import y as z)
        - visited: A list used to keep track of which files have been imported
                  this is used to prevent an import cycle that could have no end
        - filenames: A list of strings used as a stack to maintain the current file being
                     visited
        - module_stack: A list of Module PyAST nodes used as a stack to maintain the current module
                     being visited.
        - classes: A dictionary of class names and their associated functions.
        - var_count: An int used when CAST variables need to be generated (i.e. loop variables, etc)
        - global_identifier_dict: A dictionary used to map global variables to unique identifiers
        - legacy: A flag used to determine whether we generate old style CAST (uses strings for function def names)
                  or new style CAST (uses Name CAST nodes for function def names)
        """

        self.aliases = {}
        self.visited = set()
        self.filenames = [file_name.split(".")[0]]
        self.module_stack = []
        self.classes = {}
        self.var_count = 0
        self.global_identifier_dict = {}
        self.id_count = 0
        self.legacy = legacy

    def insert_next_id(self, scope_dict: Dict, dict_key: str):
        """Given a scope_dictionary and a variable name as a key,
        we insert a new key_value pair for the scope dictionary
        The ID that we inserted gets returned because some visitors
        need the ID for some additional work. In the cases where the returned
        ID isn't needed it gets ignored.

        Args:
            scope_dict (Dict): _description_
            dict_key (str): _description_
        """
        new_id_to_insert = self.id_count
        scope_dict[dict_key] = new_id_to_insert
        self.id_count += 1
        return new_id_to_insert

    def insert_alias(self, originString, alias: String):
        """Inserts an alias into a dictionary that keeps track of aliases for
            names that are aliased. For example, the following import
            import numpy as np
            np is an alias for the original name numpy

        Args:
            original (String): The original name that is being aliased
            alias    (String): The alias of the original name
        """
        # TODO
        pass

    def check_alias(self, name: String):
        """Given a python string that represents a name,
        this function checks to see if that name is an alias
        for a different name, and returns it if it is indeed an alias.
        Otherwise, the original name is returned.
        """
        if name in self.aliases:
            return self.aliases[name]
        else:
            return name

    def identify_piece(
        self,
        piece: AstNode,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """This function is used to 'centralize' the handling of different node types
        in list/dictionary/set comprehensions.
        Take the following list comprehensions as examples
        L = [ele**2 for small_l in d         for ele in small_l]   -- comp1
        L = [ele**2 for small_l in foo.bar() for ele in small_l]   -- comp2
        L = [ele**2 for small_l in foo.baz   for ele in small_l]   -- comp3
               F1         F2        F3            F4      F5

        In these comprehensions F3 has a different type for its node
            - In comp1 it's a list
            - In comp2 it's an attribute of an object with a function call
            - In comp3 it's an attribute of an object without a function call

        The code that handles comprehensions generates slightly different AST depending
        on what type these fields (F1 through F5) are, but this handling becomes very repetitive
        and difficult to maintain if it's written in the comprehension visitors. Thus, this method
        is to contain that handling in one place. This method acts on one field at a time, and thus will
        be called multiple times per comprehension as necessary.

        Args:
            piece (AstNode): The current Python AST node we're looking at, generally an individual field
                             of the list comprehension
            prev_scope_id_dict (Dict): Scope dictionaries in case something needs to be accessed or changed
            curr_scope_id_dict (Dict): see above

        [ELT for TARGET in ITER]
          F1       F2      F3
        F1 - doesn't need to be handled here because that's just code that is done somewhere else
        F2/F4 - commonly it's a Name or a Tuple node
        F3/F5 - generally a list, or something that gives back a list like:
                * a subscript
                * an attribute of an object with or w/out a function call
        """
        if isinstance(piece, ast.Tuple):  # for targets (generator.target)
            return piece
        elif isinstance(piece, ast.Name):
            ref = [
                self.filenames[-1],
                piece.col_offset,
                piece.end_col_offset,
                piece.lineno,
                piece.end_lineno,
            ]
            # return ast.Name(id=piece.id, ctx=ast.Store(), col_offset=None, end_col_offset=None, lineno=None, end_lineno=None)
            return ast.Name(
                id=piece.id,
                ctx=ast.Store(),
                col_offset=ref[1],
                end_col_offset=ref[2],
                lineno=ref[3],
                end_lineno=ref[4],
            )
        elif isinstance(piece, ast.Subscript):  # for iters (generator.iter)
            return piece.value
        elif isinstance(piece, ast.Call):
            return piece.func
        else:
            return piece

    def find_function(module_node: ast.Module, f_name: str):
        """Given a PyAST Module node, we search for a particular FunctionDef node
        which is given to us by its function name f_name.

        This function searches at the top level, that is it only searches FunctionDefs that
        exist at the module level, and will not search deeper for functions within functions.
        """
        for stmt in module_node.body:
            if isinstance(stmt, ast.FunctionDef) and stmt.name == f_name:
                return stmt

        return None

    @singledispatchmethod
    def visit(
        self, node: AstNode, prev_scope_id_dict: Dict, curr_scope_id_dict: Dict
    ):
        # print(f"Trying to visit a node of type {type(node)} but a visitor doesn't exist")
        # if(node != None):
        #    print(f"This is at line {node.lineno}")
        pass

    @visit.register
    def visit_JoinedStr(
        self,
        node: ast.JoinedStr,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        # print("JoinedStr not generating CAST yet")
        str_pieces = []
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        for s in node.values:
            source_code_data_type = ["Python", "3.8", str(type("str"))]
            if isinstance(s, ast.Str):
                str_pieces.append(
                    LiteralValue(
                        StructureType.LIST, s.s, source_code_data_type, ref
                    )
                )
            else:
                f_string_val = self.visit(
                    s.value, prev_scope_id_dict, curr_scope_id_dict
                )
                str_pieces.append(
                    LiteralValue(
                        StructureType.LIST,
                        f_string_val,
                        source_code_data_type,
                        ref,
                    )
                )

        unique_name = construct_unique_name(self.filenames[-1], "Concatenate")
        if unique_name not in prev_scope_id_dict.keys():
            # If a built-in is called, then it gets added to the global dictionary if
            # it hasn't been called before. This is to maintain one consistent ID per built-in
            # function
            if unique_name not in self.global_identifier_dict.keys():
                self.insert_next_id(self.global_identifier_dict, unique_name)

            prev_scope_id_dict[unique_name] = self.global_identifier_dict[
                unique_name
            ]
        return [
            Call(
                Name(
                    "Concatenate",
                    id=prev_scope_id_dict[unique_name],
                    source_refs=ref,
                ),
                str_pieces,
                source_refs=ref,
            )
        ]

    @visit.register
    def visit_GeneratorExp(
        self,
        node: ast.GeneratorExp,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        ref = [
            node.col_offset,
            node.end_col_offset,
            node.lineno,
            node.end_lineno,
        ]
        to_visit = ast.ListComp(
            elt=node.elt,
            generators=node.generators,
            lineno=ref[2],
            col_offset=ref[0],
            end_lineno=ref[3],
            end_col_offset=ref[1],
        )

        return self.visit(to_visit, prev_scope_id_dict, curr_scope_id_dict)

    @visit.register
    def visit_Delete(
        self,
        node: ast.Delete,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        # print("Delete not generating CAST yet")
        source_code_data_type = ["Python", "3.8", "List"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        return [
            LiteralValue(
                StructureType.LIST,
                "NotImplemented",
                source_code_data_type,
                ref,
            )
        ]

    @visit.register
    def visit_Ellipsis(
        self,
        node: ast.Ellipsis,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        source_code_data_type = ["Python", "3.8", "Ellipsis"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        return [
            LiteralValue(
                ScalarType.ELLIPSIS, "...", source_code_data_type, ref
            )
        ]

    @visit.register
    def visit_Slice(
        self,
        node: ast.Slice,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        # print("Slice not generating CAST yet")
        source_code_data_type = ["Python", "3.8", "List"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=-1,
                col_end=-1,
                row_start=-1,
                row_end=-1,
            )
        ]
        return [
            LiteralValue(
                StructureType.LIST,
                "NotImplemented",
                source_code_data_type,
                ref,
            )
        ]

    @visit.register
    def visit_ExtSlice(
        self,
        node: ast.ExtSlice,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        # print("ExtSlice not generating CAST yet")
        source_code_data_type = ["Python", "3.8", "List"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=-1,
                col_end=-1,
                row_start=-1,
                row_end=-1,
            )
        ]
        return [
            LiteralValue(
                StructureType.LIST,
                "NotImplemented",
                source_code_data_type,
                ref,
            )
        ]

    @visit.register
    def visit_Assign(
        self,
        node: ast.Assign,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Assign node, and returns its CAST representation.
        Either the assignment is simple, like x = {expression},
        or the assignment is complex, like x = y = z = ... {expression}
        Which determines how we generate the CAST for this node.

        Args:
            node (ast.Assign): A PyAST Assignment node.

        Returns:
            Assignment: An assignment CAST node
        """

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        left = []
        right = []

        if (
            len(node.targets) == 1
        ):  # x = 1, or maybe x = y, in general x = {expression}

            if isinstance(
                node.targets[0], ast.Subscript
            ):  # List subscript nodes get replaced out by
                # A function call to a "list_set"
                sub_node = node.targets[0]
                if isinstance(node.value, ast.Subscript):
                    unique_name = construct_unique_name(
                        self.filenames[-1], "_get"
                    )
                    if unique_name not in prev_scope_id_dict.keys():
                        # If a built-in is called, then it gets added to the global dictionary if
                        # it hasn't been called before. This is to maintain one consistent ID per built-in
                        # function
                        if (
                            unique_name
                            not in self.global_identifier_dict.keys()
                        ):
                            self.insert_next_id(
                                self.global_identifier_dict, unique_name
                            )

                        prev_scope_id_dict[
                            unique_name
                        ] = self.global_identifier_dict[unique_name]
                    idx = self.visit(
                        node.value.slice,
                        prev_scope_id_dict,
                        curr_scope_id_dict,
                    )[0]
                    val = self.visit(
                        node.value.value,
                        prev_scope_id_dict,
                        curr_scope_id_dict,
                    )[0]
                    args = [val, idx]

                    val = Call(
                        Name(
                            "_get",
                            id=prev_scope_id_dict[unique_name],
                            source_refs=ref,
                        ),
                        args,
                        source_refs=ref,
                    )
                else:
                    val = self.visit(
                        node.value, prev_scope_id_dict, curr_scope_id_dict
                    )[0]

                idx = self.visit(
                    sub_node.slice, prev_scope_id_dict, curr_scope_id_dict
                )[0]
                list_name = self.visit(
                    sub_node.value, prev_scope_id_dict, curr_scope_id_dict
                )[0]
                # print("-------------")
                # print(type(node.value))
                # print(type(sub_node.slice))
                # print(type(sub_node.value))
                # print("-------------")

                """
                if isinstance(arg, ast.Subscript):
                    unique_name = construct_unique_name(self.filenames[-1], "_List_get")
                    if unique_name not in prev_scope_id_dict.keys():
                        # If a built-in is called, then it gets added to the global dictionary if
                        # it hasn't been called before. This is to maintain one consistent ID per built-in
                        # function
                        if unique_name not in self.global_identifier_dict.keys():
                            self.insert_next_id(self.global_identifier_dict, unique_name)

                        prev_scope_id_dict[unique_name] = self.global_identifier_dict[unique_name]
                    idx = self.visit(arg.slice, prev_scope_id_dict, curr_scope_id_dict)[0]
                    val = self.visit(arg.value, prev_scope_id_dict, curr_scope_id_dict)[0]
                    args = [val, idx]

                    func_args.extend([Call(Name("_List_get", id=prev_scope_id_dict[unique_name], source_refs=ref), args, source_refs=ref)])
                """
                # In the case we're calling a function that doesn't have an identifier already
                # This should only be the case for built-in python functions (i.e print, len, etc...)
                # Otherwise it would be an error to call a function before it is defined
                # (An ID would exist for a user-defined function here even if it isn't visited yet because of deferment)
                unique_name = construct_unique_name(self.filenames[-1], "_set")
                if unique_name not in prev_scope_id_dict.keys():

                    # If a built-in is called, then it gets added to the global dictionary if
                    # it hasn't been called before. This is to maintain one consistent ID per built-in
                    # function
                    if unique_name not in self.global_identifier_dict.keys():
                        self.insert_next_id(
                            self.global_identifier_dict, unique_name
                        )

                    prev_scope_id_dict[
                        unique_name
                    ] = self.global_identifier_dict[unique_name]

                args = [list_name, idx, val]
                return [
                    Assignment(
                        Var(val=list_name, type="Any", source_refs=ref),
                        Call(
                            Name(
                                "_set",
                                id=prev_scope_id_dict[unique_name],
                                source_refs=ref,
                            ),
                            args,
                            source_refs=ref,
                        ),
                        source_refs=ref,
                    )
                ]

            if isinstance(node.value, ast.Subscript):

                # In the case we're calling a function that doesn't have an identifier already
                # This should only be the case for built-in python functions (i.e print, len, etc...)
                # Otherwise it would be an error to call a function before it is defined
                # (An ID would exist for a user-defined function here even if it isn't visited yet because of deferment)
                unique_name = construct_unique_name(self.filenames[-1], "_get")
                if unique_name not in prev_scope_id_dict.keys():

                    # If a built-in is called, then it gets added to the global dictionary if
                    # it hasn't been called before. This is to maintain one consistent ID per built-in
                    # function
                    if unique_name not in self.global_identifier_dict.keys():
                        self.insert_next_id(
                            self.global_identifier_dict, unique_name
                        )

                    prev_scope_id_dict[
                        unique_name
                    ] = self.global_identifier_dict[unique_name]

                var_name = self.visit(
                    node.targets[0], prev_scope_id_dict, curr_scope_id_dict
                )[0]
                idx = self.visit(
                    node.value.slice, prev_scope_id_dict, curr_scope_id_dict
                )[0]
                val = self.visit(
                    node.value.value, prev_scope_id_dict, curr_scope_id_dict
                )[0]
                args = [val, idx]
                return [
                    Assignment(
                        var_name,
                        Call(
                            Name(
                                "_get",
                                id=prev_scope_id_dict[unique_name],
                                source_refs=ref,
                            ),
                            args,
                            source_refs=ref,
                        ),
                        source_refs=ref,
                    )
                ]

            if isinstance(
                node.value, ast.BinOp
            ):  # Checking if we have an assignment of the form
                # x = LIST * NUM or x = NUM * LIST
                binop = node.value
                list_node = None
                operand = None
                if isinstance(binop.left, ast.List):
                    list_node = binop.left
                    operand = binop.right
                elif isinstance(binop.right, ast.List):
                    list_node = binop.right
                    operand = binop.left

                if list_node is not None:
                    cons = ValueConstructor()
                    lit_type = (
                        ScalarType.ABSTRACTFLOAT
                        if type(list_node.elts[0].value) == float
                        else ScalarType.INTEGER
                    )
                    cons.dim = None
                    t = get_op(binop.op)
                    cons.operator = (
                        "*"
                        if get_op(binop.op) == "Mult"
                        else "+"
                        if get_op(binop.op) == "Add"
                        else None
                    )
                    cons.size = self.visit(
                        operand, prev_scope_id_dict, curr_scope_id_dict
                    )[0]
                    cons.initial_value = LiteralValue(
                        value_type=lit_type,
                        value=list_node.elts[0].value,
                        source_code_data_type=["Python", "3.8", "Float"],
                        source_refs=ref,
                    )

                    # TODO: Source code data type metadata
                    to_ret = LiteralValue(
                        value_type="List[Any]",
                        value=cons,
                        source_code_data_type=["Python", "3.8", "List"],
                        source_refs=ref,
                    )
                    unique_name = construct_unique_name(
                        self.filenames[-1], "_get"
                    )
                    if unique_name not in prev_scope_id_dict.keys():

                        # If a built-in is called, then it gets added to the global dictionary if
                        # it hasn't been called before. This is to maintain one consistent ID per built-in
                        # function
                        if (
                            unique_name
                            not in self.global_identifier_dict.keys()
                        ):
                            self.insert_next_id(
                                self.global_identifier_dict, unique_name
                            )

                        prev_scope_id_dict[
                            unique_name
                        ] = self.global_identifier_dict[unique_name]

                    # TODO: Augment this _List_num constructor with the following
                    # First argument should be a list with the initial amount of elements
                    # Then second arg is how many times to repeat that
                    # When we say List for the first argument: It should be a literal value List that holds the elements
                    to_ret = Call(
                        Name(
                            "_List_num",
                            id=prev_scope_id_dict[unique_name],
                            source_refs=ref,
                        ),
                        [cons.initial_value, cons.size],
                        source_refs=ref,
                    )

                    # print(to_ret)
                    l_visit = self.visit(
                        node.targets[0], prev_scope_id_dict, curr_scope_id_dict
                    )
                    left.extend(l_visit)
                    return [Assignment(left[0], to_ret, source_refs=ref)]

            l_visit = self.visit(
                node.targets[0], prev_scope_id_dict, curr_scope_id_dict
            )
            r_visit = self.visit(
                node.value, prev_scope_id_dict, curr_scope_id_dict
            )
            left.extend(l_visit)
            right.extend(r_visit)
        elif (
            len(node.targets) > 1
        ):  # x = y = z = ... {Expression} (multiple assignments in one line)
            left.extend(
                self.visit(
                    node.targets[0], prev_scope_id_dict, curr_scope_id_dict
                )
            )
            node.targets = node.targets[1:]
            right.extend(
                self.visit(node, prev_scope_id_dict, curr_scope_id_dict)
            )
        else:
            raise ValueError(
                f"Unexpected number of targets for node: {len(node.targets)}"
            )

        # ref = [SourceRef(source_file_name=self.filenames[-1], col_start=node.col_offset, col_end=node.end_col_offset, row_start=node.lineno, row_end=node.end_lineno)]

        if isinstance(node.value, ast.DictComp):
            to_ret = []
            to_ret.extend(right)
            to_ret.extend(
                [
                    Assignment(
                        left[0],
                        Name(
                            name="dict__temp_",
                            id=curr_scope_id_dict["dict__temp_"],
                        ),
                        source_refs=ref,
                    )
                ]
            )
            return to_ret
        if isinstance(node.value, ast.ListComp):
            to_ret = []
            to_ret.extend(right)
            to_ret.extend(
                [
                    Assignment(
                        left[0],
                        Name(
                            name="list__temp_",
                            id=curr_scope_id_dict["list__temp_"],
                        ),
                        source_refs=ref,
                    )
                ]
            )
            return to_ret
        else:
            return [Assignment(left[0], right[0], source_refs=ref)]

    @visit.register
    def visit_Attribute(
        self,
        node: ast.Attribute,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Attribute node, which is used when accessing
        the attribute of a class. Whether it's a field or method of a class.

        Args:
            node (ast.Attribute): A PyAST Attribute node

        Returns:
            Attribute: A CAST Attribute node representing an Attribute access
        """
        # node.value and node.attr
        # node.value is some kind of AST node
        # node.attr is a string (or perhaps name)

        # node.value.id gets us module name (string)
        # node.attr gets us attribute we're accessing (string)
        # helper(node.attr) -> "module_name".node.attr

        # x.T -> node.value: the node x (Name) -> node.attr is just "T"

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        value_cast = self.visit(
            node.value, prev_scope_id_dict, curr_scope_id_dict
        )
        unique_name = (
            node.attr
        )  # TODO: This unique name might change to better reflect what it belongs to (i.e. x.T instead of just T)

        if isinstance(node.ctx, ast.Load):
            if unique_name not in curr_scope_id_dict:
                if unique_name in prev_scope_id_dict:
                    curr_scope_id_dict[unique_name] = prev_scope_id_dict[
                        unique_name
                    ]
                else:
                    if (
                        unique_name not in self.global_identifier_dict
                    ):  # added for random.seed not exising, and other modules like that. in other words for functions in modules that we don't have visibility for.
                        self.insert_next_id(
                            self.global_identifier_dict, unique_name
                        )
                    curr_scope_id_dict[
                        unique_name
                    ] = self.global_identifier_dict[unique_name]
        if isinstance(node.ctx, ast.Store):
            if unique_name not in curr_scope_id_dict:
                if unique_name in prev_scope_id_dict:
                    curr_scope_id_dict[unique_name] = prev_scope_id_dict[
                        unique_name
                    ]
                else:
                    self.insert_next_id(curr_scope_id_dict, unique_name)

        attr_cast = Name(
            name=node.attr, id=curr_scope_id_dict[unique_name], source_refs=ref
        )

        return [Attribute(value_cast[0], attr_cast, source_refs=ref)]

    @visit.register
    def visit_AugAssign(
        self,
        node: ast.AugAssign,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST AugAssign node, which is used for an
        augmented assignment, like x += 1. AugAssign node is converted
        to a regular PyAST Assign node and passed to that visitor to
        generate CAST.

        Args:
            node (ast.AugAssign): A PyAST AugAssign node

        Returns:
            Assign: A CAST Assign node, generated by the Assign visitor.
        """

        # Convert AugAssign to regular Assign, and visit
        target = node.target
        value = node.value

        if isinstance(target, ast.Attribute):
            convert = ast.Assign(
                targets=[target],
                value=ast.BinOp(
                    left=target,
                    op=node.op,
                    right=value,
                    col_offset=node.col_offset,
                    end_col_offset=node.end_col_offset,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno,
                ),
                col_offset=node.col_offset,
                end_col_offset=node.end_col_offset,
                lineno=node.lineno,
                end_lineno=node.end_lineno,
            )
        elif isinstance(target, ast.Subscript):
            convert = ast.Assign(
                targets=[target],
                value=ast.BinOp(
                    left=target,
                    ctx=ast.Load(),
                    op=node.op,
                    right=value,
                    col_offset=node.col_offset,
                    end_col_offset=node.end_col_offset,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno,
                ),
                col_offset=node.col_offset,
                end_col_offset=node.end_col_offset,
                lineno=node.lineno,
                end_lineno=node.end_lineno,
            )
        else:
            convert = ast.Assign(
                targets=[target],
                value=ast.BinOp(
                    left=ast.Name(
                        target.id,
                        ctx=ast.Load(),
                        col_offset=node.col_offset,
                        end_col_offset=node.end_col_offset,
                        lineno=node.lineno,
                        end_lineno=node.end_lineno,
                    ),
                    op=node.op,
                    right=value,
                    col_offset=node.col_offset,
                    end_col_offset=node.end_col_offset,
                    lineno=node.lineno,
                    end_lineno=node.end_lineno,
                ),
                col_offset=node.col_offset,
                end_col_offset=node.end_col_offset,
                lineno=node.lineno,
                end_lineno=node.end_lineno,
            )

        return self.visit(convert, prev_scope_id_dict, curr_scope_id_dict)

    @visit.register
    def visit_BinOp(
        self,
        node: ast.BinOp,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST BinOp node, which consists of all the arithmetic
        and bitwise operators.

        Args:
            node (ast.BinOp): A PyAST Binary operator node

        Returns:
            BinaryOp: A CAST binary operator node representing a math
                      operation (arithmetic or bitwise)
        """

        left = self.visit(node.left, prev_scope_id_dict, curr_scope_id_dict)
        op = get_op(node.op)
        right = self.visit(node.right, prev_scope_id_dict, curr_scope_id_dict)

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        leftb = []
        rightb = []

        if len(left) > 1:
            leftb = left[0:-1]
        if len(right) > 1:
            rightb = right[0:-1]

        return (
            leftb
            + rightb
            + [BinaryOp(op, left[-1], right[-1], source_refs=ref)]
        )

    @visit.register
    def visit_Break(
        self,
        node: ast.Break,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Break node, which is just a break statement
           nothing to be done for a Break node, just return a ModelBreak()
           object

        Args:
            node (ast.Break): An AST Break node

        Returns:
            ModelBreak: A CAST Break node

        """

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        return [ModelBreak(source_refs=ref)]

    @visit.register
    def visit_BoolOp(
        self,
        node: ast.BoolOp,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a BoolOp node, which is a boolean operation connected with 'and'/'or's
           The BoolOp node gets converted into an AST Compare node, and then the work is
           passed off to it.

        Args:
            node (ast.BoolOp): An AST BoolOp node

        Returns:
            BinaryOp: A BinaryOp node that is composed of operations connected with 'and'/'or's

        """
        op = node.op
        vals = node.values
        bool_ops = [node.op for i in range(len(vals) - 1)]
        ref = [
            self.filenames[-1],
            node.col_offset,
            node.end_col_offset,
            node.lineno,
            node.end_lineno,
        ]

        compare_op = ast.Compare(
            left=vals[0],
            ops=bool_ops,
            comparators=vals[1:],
            col_offset=node.col_offset,
            end_col_offset=node.end_col_offset,
            lineno=node.lineno,
            end_lineno=node.end_lineno,
        )
        return self.visit(compare_op, prev_scope_id_dict, curr_scope_id_dict)

    @visit.register
    def visit_Call(
        self,
        node: ast.Call,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Call node, which represents a function call.
        Special care must be taken to see if it's a function call or a class's
        method call. The CAST is generated a little different depending on
        what kind of call it is.

        Args:
            node (ast.Call): a PyAST Call node

        Returns:
            Call: A CAST function call node
        """

        args = []
        func_args = []
        kw_args = []
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        if len(node.args) > 0:
            for arg in node.args:
                if isinstance(arg, ast.Subscript):
                    unique_name = construct_unique_name(
                        self.filenames[-1], "_get"
                    )
                    if unique_name not in prev_scope_id_dict.keys():
                        # If a built-in is called, then it gets added to the global dictionary if
                        # it hasn't been called before. This is to maintain one consistent ID per built-in
                        # function
                        if (
                            unique_name
                            not in self.global_identifier_dict.keys()
                        ):
                            self.insert_next_id(
                                self.global_identifier_dict, unique_name
                            )

                        prev_scope_id_dict[
                            unique_name
                        ] = self.global_identifier_dict[unique_name]
                    idx = self.visit(
                        arg.slice, prev_scope_id_dict, curr_scope_id_dict
                    )[0]
                    val = self.visit(
                        arg.value, prev_scope_id_dict, curr_scope_id_dict
                    )[0]
                    args = [val, idx]

                    func_args.extend(
                        [
                            Call(
                                Name(
                                    "_get",
                                    id=prev_scope_id_dict[unique_name],
                                    source_refs=ref,
                                ),
                                args,
                                source_refs=ref,
                            )
                        ]
                    )
                elif isinstance(arg, ast.Starred):
                    if isinstance(arg.value, ast.Subscript):
                        func_args.append(
                            Name(
                                name=arg.value.value.id, id=-1, source_refs=ref
                            )
                        )
                    else:
                        func_args.append(
                            Name(name=arg.value.id, id=-1, source_refs=ref)
                        )
                else:
                    res = self.visit(
                        arg, prev_scope_id_dict, curr_scope_id_dict
                    )
                    if res != None:
                        func_args.extend(res)

        # g(3,id=4) TODO: Think more about this
        if len(node.keywords) > 0:
            for arg in node.keywords:
                # print(prev_scope_id_dict)
                # print(curr_scope_id_dict)
                if arg.arg != None:
                    val = self.visit(
                        arg.value, prev_scope_id_dict, curr_scope_id_dict
                    )[0]
                    assign_node = Assignment(
                        left=Var(
                            Name(name=arg.arg, id=-1, source_refs=ref),
                            type="float",
                            source_refs=ref,
                        ),
                        right=val,
                        source_refs=ref,
                    )
                elif isinstance(arg.value, ast.Dict):
                    val = self.visit(
                        arg.value, prev_scope_id_dict, curr_scope_id_dict
                    )[0]
                    assign_node = val
                else:
                    if isinstance(arg.value, ast.Attribute) and isinstance(
                        arg.value.value, ast.Attribute
                    ):
                        assign_node = Name(
                            name=arg.value.value.attr, id=-1, source_refs=ref
                        )
                    elif isinstance(arg.value, ast.Call):
                        assign_node = Name(
                            name=arg.value.func.id, id=-1, source_refs=ref
                        )
                    else:
                        assign_node = Name(
                            name=arg.value.id, id=-1, source_refs=ref
                        )
                kw_args.append(assign_node)
                # kw_args.extend(self.visit(arg.value, prev_scope_id_dict, curr_scope_id_dict))

        args = func_args + kw_args

        if isinstance(node.func, ast.Attribute):
            res = self.visit(node.func, prev_scope_id_dict, curr_scope_id_dict)
            return [Call(res[0], args, source_refs=ref)]
        else:
            # In the case we're calling a function that doesn't have an identifier already
            # This should only be the case for built-in python functions (i.e print, len, etc...)
            # Otherwise it would be an error to call a function before it is defined
            # (An ID would exist for a user-defined function here even if it isn't visited yet because of deferment)
            if isinstance(node.func, ast.Call):
                if node.func.func.id == "list":
                    unique_name = construct_unique_name(
                        self.filenames[-1], "cast"
                    )
                else:
                    unique_name = construct_unique_name(
                        self.filenames[-1], node.func.func.id
                    )
            else:
                if node.func.id == "list":
                    unique_name = construct_unique_name(
                        self.filenames[-1], "cast"
                    )
                else:
                    unique_name = construct_unique_name(
                        self.filenames[-1], node.func.id
                    )
            if unique_name not in prev_scope_id_dict.keys():

                # If a built-in is called, then it gets added to the global dictionary if
                # it hasn't been called before. This is to maintain one consistent ID per built-in
                # function
                if unique_name not in self.global_identifier_dict.keys():
                    self.insert_next_id(
                        self.global_identifier_dict, unique_name
                    )

                prev_scope_id_dict[unique_name] = self.global_identifier_dict[
                    unique_name
                ]

            if isinstance(node.func, ast.Call):
                if node.func.func.id == "list":
                    args.append(
                        LiteralValue(
                            StructureType.LIST,
                            node.func.func.id,
                            ["Python", "3.8", "List"],
                            ref,
                        )
                    )
                    return [
                        Call(
                            Name(
                                "cast",
                                id=prev_scope_id_dict[unique_name],
                                source_refs=ref,
                            ),
                            args,
                            source_refs=ref,
                        )
                    ]
                else:
                    return [
                        Call(
                            Name(
                                node.func.func.id,
                                id=prev_scope_id_dict[unique_name],
                                source_refs=ref,
                            ),
                            args,
                            source_refs=ref,
                        )
                    ]
            else:
                if node.func.id == "list":
                    args.append(
                        LiteralValue(
                            StructureType.LIST,
                            node.func.id,
                            ["Python", "3.8", "List"],
                            ref,
                        )
                    )
                    return [
                        Call(
                            Name(
                                "cast",
                                id=prev_scope_id_dict[unique_name],
                                source_refs=ref,
                            ),
                            args,
                            source_refs=ref,
                        )
                    ]
                else:
                    return [
                        Call(
                            Name(
                                node.func.id,
                                id=prev_scope_id_dict[unique_name],
                                source_refs=ref,
                            ),
                            args,
                            source_refs=ref,
                        )
                    ]

    def collect_fields(
        self, node: ast.FunctionDef, prev_scope_id_dict, curr_scope_id_dict
    ):
        """Attempts to solve the problem of collecting any additional fields
        for a class that get created in functions outside of __init__
        """
        fields = []
        for n in node.body:
            if isinstance(n, ast.Assign) and isinstance(
                n.targets[0], ast.Attribute
            ):
                attr_node = n.targets[0]
                if isinstance(attr_node.value, ast.Attribute):
                    if attr_node.value.value == "self":
                        ref = [
                            SourceRef(
                                source_file_name=self.filenames[-1],
                                col_start=attr_node.col_offset,
                                col_end=attr_node.end_col_offset,
                                row_start=attr_node.lineno,
                                row_end=attr_node.end_lineno,
                            )
                        ]
                        # Need IDs for name, which one?
                        attr_id = self.insert_next_id(
                            curr_scope_id_dict, attr_node.value.attr
                        )
                        fields.append(
                            Var(
                                Name(
                                    attr_node.value.attr,
                                    id=attr_id,
                                    source_refs=ref,
                                ),
                                "float",
                                source_refs=ref,
                            )
                        )
                elif attr_node.value.id == "self":
                    ref = [
                        SourceRef(
                            source_file_name=self.filenames[-1],
                            col_start=attr_node.col_offset,
                            col_end=attr_node.end_col_offset,
                            row_start=attr_node.lineno,
                            row_end=attr_node.end_lineno,
                        )
                    ]
                    # Need IDs for name, which one?
                    attr_id = self.insert_next_id(
                        curr_scope_id_dict, attr_node.attr
                    )
                    fields.append(
                        Var(
                            Name(attr_node.attr, id=attr_id, source_refs=ref),
                            "float",
                            source_refs=ref,
                        )
                    )

        return fields

    @visit.register
    def visit_ClassDef(
        self,
        node: ast.ClassDef,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST ClassDef node, which is used to define user classes.
        Acquiring the fields of the class involves going through the __init__
        function and seeing if the attributes are associated with the self
        parameter. In addition, we add to the 'classes' dictionary the name of
        the class and a list of all its functions.

        Args:
            node (ast.ClassDef): A PyAST class definition node

        Returns:
            ClassDef: A CAST class definition node
        """
        name = node.name
        self.classes[name] = []

        bases = []
        for base in node.bases:
            bases.extend(
                self.visit(base, prev_scope_id_dict, curr_scope_id_dict)
            )

        fields = []
        funcs = []
        for func in node.body:
            if isinstance(func, ast.FunctionDef):
                if func.name != "__init__":
                    fields.extend(
                        self.collect_fields(
                            func, prev_scope_id_dict, curr_scope_id_dict
                        )
                    )
                funcs.extend(
                    self.visit(func, prev_scope_id_dict, curr_scope_id_dict)
                )
                # if isinstance(func,ast.FunctionDef):
                self.classes[name].append(func.name)
                self.insert_next_id(prev_scope_id_dict, name)

        # Get the fields in the class from init
        init_func = None
        for f in node.body:
            if isinstance(f, ast.FunctionDef) and f.name == "__init__":
                init_func = f.body
                break

        if init_func != None:
            for func_node in init_func:
                if isinstance(func_node, ast.Assign) and isinstance(
                    func_node.targets[0], ast.Attribute
                ):
                    attr_node = func_node.targets[0]
                    if attr_node.value.id == "self":
                        ref = [
                            SourceRef(
                                source_file_name=self.filenames[-1],
                                col_start=attr_node.col_offset,
                                col_end=attr_node.end_col_offset,
                                row_start=attr_node.lineno,
                                row_end=attr_node.end_lineno,
                            )
                        ]
                        # Need IDs for name, which one?
                        attr_id = self.insert_next_id(
                            curr_scope_id_dict, attr_node.attr
                        )
                        fields.append(
                            Var(
                                Name(
                                    attr_node.attr, id=attr_id, source_refs=ref
                                ),
                                "float",
                                source_refs=ref,
                            )
                        )

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        return [RecordDef(name, bases, funcs, fields, source_refs=ref)]

    @visit.register
    def visit_Compare(
        self,
        node: ast.Compare,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Compare node, which consists of boolean operations

        Args:
            node (ast.Compare): A PyAST Compare node

        Returns:
            BinaryOp: A BinaryOp node, which in this case will hold a boolean
            operation
        """

        ops = {
            ast.And: BinaryOperator.AND,
            ast.Or: BinaryOperator.OR,
            ast.Eq: BinaryOperator.EQ,
            ast.NotEq: BinaryOperator.NOTEQ,
            ast.Lt: BinaryOperator.LT,
            ast.LtE: BinaryOperator.LTE,
            ast.Gt: BinaryOperator.GT,
            ast.GtE: BinaryOperator.GTE,
            ast.In: BinaryOperator.IN,
            ast.NotIn: BinaryOperator.NOTIN,
            ast.IsNot: BinaryOperator.NOTIS,
            ast.Is: BinaryOperator.IS,
        }

        # Fetch the first element (which is in left)
        left = node.left

        # Grab the first comparison operation
        op = ops[type(node.ops.pop())]

        # If we have more than one operand left, then we 'recurse' without the leftmost
        # operand and the first operator
        if len(node.comparators) > 1:
            node.left = node.comparators.pop()
            right = node
        else:
            right = node.comparators[0]

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        l = self.visit(left, prev_scope_id_dict, curr_scope_id_dict)
        r = self.visit(right, prev_scope_id_dict, curr_scope_id_dict)
        return [BinaryOp(op, l[0], r[0], source_refs=ref)]

    @visit.register
    def visit_Constant(
        self,
        node: ast.Constant,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Constant node, which can hold either numeric or
        string values. A dictionary is used to index into which operation
        we're doing.

        Args:
            node (ast.Constant): A PyAST Constant node

        Returns:
            Number: A CAST numeric node, if the node's value is an int or float
            String: A CAST string node, if the node's value is a string
            Boolean: A CAST boolean node, if the node's value is a boolean

        Raises:
            TypeError: If the node's value is something else that isn't
                       recognized by the other two cases
        """

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        source_code_data_type = ["Python", "3.8", str(type(node.value))]
        if isinstance(node.value, int):
            return [
                LiteralValue(
                    ScalarType.INTEGER, node.value, source_code_data_type, ref
                )
            ]
        elif isinstance(node.value, float):
            return [
                LiteralValue(
                    ScalarType.ABSTRACTFLOAT,
                    node.value,
                    source_code_data_type,
                    ref,
                )
            ]
        elif isinstance(node.value, bool):
            return [
                LiteralValue(
                    ScalarType.BOOLEAN, node.value, source_code_data_type, ref
                )
            ]
        elif isinstance(node.value, str):
            return [
                LiteralValue(
                    StructureType.LIST, node.value, source_code_data_type, ref
                )
            ]
        elif node.value is None:
            return [LiteralValue(None, None, source_code_data_type, ref)]
        elif isinstance(node.value, type(...)):
            return []
        else:
            raise TypeError(f"Type {str(type(node.value))} not supported")

    @visit.register
    def visit_Continue(
        self,
        node: ast.Continue,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Continue node, which is just a continue statement
           nothing to be done for a Continue node, just return a ModelContinue node

        Args:
            node (ast.Continue): An AST Continue node

        Returns:
            ModelContinue: A CAST Continue node
        """

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        return [ModelContinue(source_refs=ref)]

    @visit.register
    def visit_Dict(
        self,
        node: ast.Dict,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Dict node, which represents a dictionary.

        Args:
            node (ast.Dict): A PyAST dictionary node

        Returns:
            Dict: A CAST Dictionary node.
        """
        # TODO: when a ** shows up in a dictionary

        keys = []
        values = []
        if len(node.keys) > 0:
            for piece in node.keys:
                if piece != None:
                    keys.extend(
                        self.visit(
                            piece, prev_scope_id_dict, curr_scope_id_dict
                        )
                    )

        if len(node.values) > 0:
            for piece in node.values:
                if piece != None:
                    values.extend(
                        self.visit(
                            piece, prev_scope_id_dict, curr_scope_id_dict
                        )
                    )

        k = [e.value if hasattr(e, "value") else e for e in keys]
        v = [e.value if hasattr(e, "value") else e for e in values]

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        for key in k:
            if isinstance(key, Tuple):
                return [
                    LiteralValue(
                        StructureType.MAP,
                        "",
                        source_code_data_type=["Python", "3.8", str(dict)],
                        source_refs=ref,
                    )
                ]

        # return [LiteralValue(StructureType.MAP, str(dict(list(zip(k,v)))), source_code_data_type=["Python","3.8",str(dict)], source_refs=ref)]
        return [
            LiteralValue(
                StructureType.MAP,
                str(list(zip(k, v))),
                source_code_data_type=["Python", "3.8", str(dict)],
                source_refs=ref,
            )
        ]

    @visit.register
    def visit_Expr(
        self,
        node: ast.Expr,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Expr node, which represents some kind of standalone
        expression.

        Args:
            node (ast.Expr): A PyAST Expression node

        Returns:
            Expr:      A CAST Expression node
            [AstNode]: A list of AstNodes if the expression consists
                       of more than one node
        """

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        val = self.visit(node.value, prev_scope_id_dict, curr_scope_id_dict)
        if len(val) > 1:
            return val
        return [Expr(val[0], source_refs=ref)]

    @visit.register
    def visit_For(
        self, node: ast.For, prev_scope_id_dict: Dict, curr_scope_id_dict: Dict
    ):
        """Visits a PyAST For node, which represents Python for loops.
        A For loop needs different handling than a while loop.
        In particular, a For loop acts on an iterator as opposed to acting on
        some kind of condition. In order to make this translation a little easier to handle
        we leverage the iterator constructs to convert the For loop into a while loop using
        the iterators.

        Args:
            node (ast.For): A PyAST For loop node.

        Returns:
            Loop: A CAST loop node, which generically represents both For
                  loops and While loops.
        """

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        target = self.visit(
            node.target, prev_scope_id_dict, curr_scope_id_dict
        )[0]
        iterable = self.visit(
            node.iter, prev_scope_id_dict, curr_scope_id_dict
        )[0]

        # The body of a loop contains its own scope (it can create variables only it can see and can use
        # variables from its enclosing scope) so we copy the current scope and merge scopes
        # to create the enclosing scope for the loop body
        curr_scope_copy = copy.deepcopy(curr_scope_id_dict)
        merge_dicts(prev_scope_id_dict, curr_scope_id_dict)
        loop_scope_id_dict = {}

        # When we pass in scopes, we pass what's currently in the previous scope along with
        # the curr scope which would consist of the loop variables (node.target) and the item
        # we loop over (iter) though the second one shouldn't ever be accessed
        body = []
        for piece in node.body + node.orelse:
            body.extend(
                self.visit(piece, curr_scope_id_dict, loop_scope_id_dict)
            )

        # Once we're out of the loop body we can copy the current scope back
        curr_scope_id_dict = copy.deepcopy(curr_scope_copy)

        # TODO: Mark these as variables that were generated by this script at some point
        # (^ This was a really old request, not sure if it's still needed at this point)
        iterator_name = f"generated_iter_{self.var_count}"
        self.var_count += 1

        iterator_id = self.insert_next_id(curr_scope_id_dict, iterator_name)

        # 'iter' and 'next' are python built-ins
        iter_id = -1
        if "iter" not in self.global_identifier_dict.keys():
            iter_id = self.insert_next_id(self.global_identifier_dict, "iter")
        else:
            iter_id = self.global_identifier_dict["iter"]

        if "next" not in self.global_identifier_dict.keys():
            next_id = self.insert_next_id(self.global_identifier_dict, "next")
        else:
            next_id = self.global_identifier_dict["next"]

        stop_cond_name = f"sc_{self.var_count}"
        self.var_count += 1

        stop_cond_id = self.insert_next_id(curr_scope_id_dict, stop_cond_name)

        iter_var_cast = Var(
            Name(name=iterator_name, id=iterator_id, source_refs=ref),
            "iterator",
            source_refs=ref,
        )

        stop_cond_var_cast = Var(
            Name(name=stop_cond_name, id=stop_cond_id, source_refs=ref),
            "boolean",
            source_refs=ref,
        )

        iter_var = Assignment(
            iter_var_cast,
            Call(
                Name(name="iter", id=iter_id, source_refs=ref),
                [iterable],
                source_refs=ref,
            ),
            source_refs=ref,
        )

        first_next = Assignment(
            Tuple(
                [target, iter_var_cast, stop_cond_var_cast], source_refs=ref
            ),
            Call(
                Name(name="next", id=next_id, source_refs=ref),
                [
                    Var(
                        Name(
                            name=iterator_name, id=iterator_id, source_refs=ref
                        ),
                        "iterator",
                        source_refs=ref,
                    )
                ],
                source_refs=ref,
            ),
            source_refs=ref,
        )

        loop_cond = BinaryOp(
            op=BinaryOperator.NOTEQ,
            left=stop_cond_var_cast,
            right=LiteralValue(
                ScalarType.BOOLEAN,
                True,
                ["Python", "3.8", "boolean"],
                source_refs=ref,
            ),
            source_refs=ref,
        )

        loop_assign = Assignment(
            Tuple(
                [target, iter_var_cast, stop_cond_var_cast], source_refs=ref
            ),
            Call(
                Name(name="next", id=next_id, source_refs=ref),
                [
                    Var(
                        Name(
                            name=iterator_name, id=iterator_id, source_refs=ref
                        ),
                        "iterator",
                        source_refs=ref,
                    )
                ],
                source_refs=ref,
            ),
            source_refs=ref,
        )

        return [
            Loop(
                init=[iter_var, first_next],
                expr=loop_cond,
                body=body + [loop_assign],
                source_refs=ref,
            )
        ]

    @visit.register
    def visit_FunctionDef(
        self,
        node: ast.FunctionDef,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST FunctionDef node. Which is used for a Python
        function definition.

        Args:
            node (ast.FunctionDef): A PyAST function definition node

        Returns:
            FunctionDef: A CAST Function Definition node
        """

        # Copy the enclosing scope dictionary as it is before we visit the current function
        # The idea for this is to prevent any weird overwritting issues that may arise from modifying
        # dictionaries in place
        prev_scope_id_dict_copy = copy.deepcopy(prev_scope_id_dict)

        body = []
        args = []
        curr_scope_id_dict = {}
        arg_count = len(node.args.args)
        default_val_count = len(node.args.defaults)
        if arg_count > 0:
            # No argument has a default value
            if default_val_count == 0:
                for arg in node.args.args:
                    # unique_name = construct_unique_name(self.filenames[-1], arg.arg)
                    self.insert_next_id(curr_scope_id_dict, arg.arg)
                    # self.insert_next_id(curr_scope_id_dict, unique_name)
                    args.append(
                        Var(
                            Name(
                                arg.arg,
                                id=curr_scope_id_dict[arg.arg],
                                source_refs=[
                                    SourceRef(
                                        self.filenames[-1],
                                        arg.col_offset,
                                        arg.end_col_offset,
                                        arg.lineno,
                                        arg.end_lineno,
                                    )
                                ],
                            ),
                            "float",  # TODO: Correct typing instead of just 'float'
                            None,
                            source_refs=[
                                SourceRef(
                                    self.filenames[-1],
                                    arg.col_offset,
                                    arg.end_col_offset,
                                    arg.lineno,
                                    arg.end_lineno,
                                )
                            ],
                        )
                    )
            else:
                # Implies that all arguments have default values
                if arg_count == default_val_count:
                    for i, arg in enumerate(node.args.args, 0):
                        self.insert_next_id(curr_scope_id_dict, arg.arg)
                        val = self.visit(
                            node.args.defaults[i],
                            prev_scope_id_dict,
                            curr_scope_id_dict,
                        )[0]
                        args.append(
                            Var(
                                Name(
                                    arg.arg,
                                    id=curr_scope_id_dict[arg.arg],
                                    source_refs=[
                                        SourceRef(
                                            self.filenames[-1],
                                            arg.col_offset,
                                            arg.end_col_offset,
                                            arg.lineno,
                                            arg.end_lineno,
                                        )
                                    ],
                                ),
                                "float",  # TODO: Correct typing instead of just 'float'
                                val,
                                source_refs=[
                                    SourceRef(
                                        self.filenames[-1],
                                        arg.col_offset,
                                        arg.end_col_offset,
                                        arg.lineno,
                                        arg.end_lineno,
                                    )
                                ],
                            )
                        )

                # There's less default values than actual args, the positional-only arguments come first
                else:
                    pos_idx = 0
                    for arg in node.args.args:
                        if arg_count == default_val_count:
                            break
                        self.insert_next_id(curr_scope_id_dict, arg.arg)
                        args.append(
                            Var(
                                Name(
                                    arg.arg,
                                    id=curr_scope_id_dict[arg.arg],
                                    source_refs=[
                                        SourceRef(
                                            self.filenames[-1],
                                            arg.col_offset,
                                            arg.end_col_offset,
                                            arg.lineno,
                                            arg.end_lineno,
                                        )
                                    ],
                                ),
                                "float",  # TODO: Correct typing instead of just 'float'
                                None,
                                source_refs=[
                                    SourceRef(
                                        self.filenames[-1],
                                        arg.col_offset,
                                        arg.end_col_offset,
                                        arg.lineno,
                                        arg.end_lineno,
                                    )
                                ],
                            )
                        )

                        pos_idx += 1
                        arg_count -= 1

                    default_index = 0
                    while arg_count > 0:
                        # unique_name = construct_unique_name(self.filenames[-1], arg.arg)
                        arg = node.args.args[pos_idx]
                        self.insert_next_id(curr_scope_id_dict, arg.arg)
                        val = self.visit(
                            node.args.defaults[default_index],
                            prev_scope_id_dict,
                            curr_scope_id_dict,
                        )[0]
                        # self.insert_next_id(curr_scope_id_dict, unique_name)
                        args.append(
                            Var(
                                Name(
                                    arg.arg,
                                    id=curr_scope_id_dict[arg.arg],
                                    source_refs=[
                                        SourceRef(
                                            self.filenames[-1],
                                            arg.col_offset,
                                            arg.end_col_offset,
                                            arg.lineno,
                                            arg.end_lineno,
                                        )
                                    ],
                                ),
                                "float",  # TODO: Correct typing instead of just 'float'
                                val,
                                source_refs=[
                                    SourceRef(
                                        self.filenames[-1],
                                        arg.col_offset,
                                        arg.end_col_offset,
                                        arg.lineno,
                                        arg.end_lineno,
                                    )
                                ],
                            )
                        )

                        pos_idx += 1
                        arg_count -= 1
                        default_index += 1

        # Store '*args' as a name
        arg = node.args.vararg
        if arg != None:
            self.insert_next_id(curr_scope_id_dict, arg.arg)
            args.append(
                Var(
                    Name(
                        arg.arg,
                        id=curr_scope_id_dict[arg.arg],
                        source_refs=[
                            SourceRef(
                                self.filenames[-1],
                                arg.col_offset,
                                arg.end_col_offset,
                                arg.lineno,
                                arg.end_lineno,
                            )
                        ],
                    ),
                    "float",  # TODO: Correct typing instead of just 'float'
                    None,
                    source_refs=[
                        SourceRef(
                            self.filenames[-1],
                            arg.col_offset,
                            arg.end_col_offset,
                            arg.lineno,
                            arg.end_lineno,
                        )
                    ],
                )
            )

        # Store '**kwargs' as a name
        arg = node.args.kwarg
        if arg != None:
            self.insert_next_id(curr_scope_id_dict, arg.arg)
            args.append(
                Var(
                    Name(
                        arg.arg,
                        id=curr_scope_id_dict[arg.arg],
                        source_refs=[
                            SourceRef(
                                self.filenames[-1],
                                arg.col_offset,
                                arg.end_col_offset,
                                arg.lineno,
                                arg.end_lineno,
                            )
                        ],
                    ),
                    "float",  # TODO: Correct typing instead of just 'float'
                    None,
                    source_refs=[
                        SourceRef(
                            self.filenames[-1],
                            arg.col_offset,
                            arg.end_col_offset,
                            arg.lineno,
                            arg.end_lineno,
                        )
                    ],
                )
            )

        functions_to_visit = []

        if len(node.body) > 0:
            # To account for nested loops we check to see if the CAST node is in a list and
            # extend accordingly
            for piece in node.body:
                # We defer visiting function defs until we've cleared the rest of the code in the function
                if isinstance(piece, ast.FunctionDef):
                    self.insert_next_id(curr_scope_id_dict, piece.name)
                    prev_scope_id_dict[piece.name] = curr_scope_id_dict[
                        piece.name
                    ]
                    functions_to_visit.append(piece)
                    continue

                # Have to figure out name IDs for imports (i.e. other modules)
                # These asserts will keep us from visiting them from now
                # assert not isinstance(piece, ast.Import)
                # assert not isinstance(piece, ast.ImportFrom)
                to_add = self.visit(
                    piece, prev_scope_id_dict, curr_scope_id_dict
                )

                # TODO: Find the case where "__getitem__" is used
                if hasattr(to_add, "__iter__") or hasattr(
                    to_add, "__getitem__"
                ):
                    body.extend(to_add)
                elif to_add == None:
                    body.extend([])
                else:
                    raise TypeError(
                        f"Unexpected type in visit_FuncDef: {type(to_add)}"
                    )

            # Merge keys from prev_scope not in cur_scope into cur_scope
            merge_dicts(prev_scope_id_dict, curr_scope_id_dict)

            # Visit the deferred functions
            for piece in functions_to_visit:
                to_add = self.visit(piece, curr_scope_id_dict, {})
                body.extend(to_add)

        # TODO: Decorators? Returns? Type_comment?
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        # "Revert" the enclosing scope dictionary to what it was before we went into this function
        # since none of the variables within here should exist outside of here..?
        # TODO: this might need to be different, since Python variables can exist outside of a scope??
        prev_scope_id_dict = copy.deepcopy(prev_scope_id_dict_copy)

        # Global level (i.e. module level) functions have their module names appended to them, we make sure
        # we have the correct name depending on whether or not we're visiting a global
        # level function or a function enclosed within another function
        if node.name in prev_scope_id_dict.keys():
            if self.legacy:
                return [FunctionDef(node.name, args, body, source_refs=ref)]
            else:
                return [
                    FunctionDef(
                        Name(
                            node.name,
                            prev_scope_id_dict[node.name],
                            source_refs=ref,
                        ),
                        args,
                        body,
                        source_refs=ref,
                    )
                ]
        else:
            unique_name = construct_unique_name(self.filenames[-1], node.name)
            if unique_name in prev_scope_id_dict.keys():
                if self.legacy:
                    return [
                        FunctionDef(node.name, args, body, source_refs=ref)
                    ]
                else:
                    return [
                        FunctionDef(
                            Name(
                                node.name,
                                prev_scope_id_dict[unique_name],
                                source_refs=ref,
                            ),
                            args,
                            body,
                            source_refs=ref,
                        )
                    ]
            else:
                self.insert_next_id(prev_scope_id_dict, unique_name)
                return [
                    FunctionDef(
                        Name(
                            node.name,
                            prev_scope_id_dict[unique_name],
                            source_refs=ref,
                        ),
                        args,
                        body,
                        source_refs=ref,
                    )
                ]

    @visit.register
    def visit_Lambda(
        self,
        node: ast.Lambda,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Lambda node. Which is used for a Python Lambda
        function definition. It works pretty analogously to the FunctionDef
        node visitor. It also returns a FunctionDef node like the PyAST
        FunctionDef node visitor.

        Args:
            node (ast.Lambda): A PyAST lambda function definition node

        Returns:
            FunctionDef: A CAST Function Definition node

        """

        curr_scope_id_dict = {}

        args = []
        # TODO: Correct typing instead of just 'float'
        if len(node.args.args) > 0:
            for arg in node.args.args:
                self.insert_next_id(curr_scope_id_dict, arg.arg)

                args.append(
                    Var(
                        Name(
                            arg.arg,
                            id=curr_scope_id_dict[arg.arg],
                            source_refs=[
                                SourceRef(
                                    self.filenames[-1],
                                    arg.col_offset,
                                    arg.end_col_offset,
                                    arg.lineno,
                                    arg.end_lineno,
                                )
                            ],
                        ),
                        "float",  # TODO: Correct typing instead of just 'float'
                        source_refs=[
                            SourceRef(
                                self.filenames[-1],
                                arg.col_offset,
                                arg.end_col_offset,
                                arg.lineno,
                                arg.end_lineno,
                            )
                        ],
                    )
                )

        body = self.visit(node.body, prev_scope_id_dict, curr_scope_id_dict)

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        # TODO: add an ID for lambda name
        if self.legacy:
            return [FunctionDef("LAMBDA", args, body, source_refs=ref)]
        else:
            return [
                FunctionDef(Name("LAMBDA", id=-1), args, body, source_refs=ref)
            ]

    @visit.register
    def visit_ListComp(
        self,
        node: ast.ListComp,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST ListComp node, which are used for Python list comprehensions.
        List comprehensions generate a list from some generator expression.

        Args:
            node (ast.ListComp): A PyAST list comprehension node

        Returns:
            Loop:
        """

        ref = [
            self.filenames[-1],
            node.col_offset,
            node.end_col_offset,
            node.lineno,
            node.end_lineno,
        ]

        temp_list_name = f"list__temp_"
        temp_assign = ast.Assign(
            targets=[
                ast.Name(
                    id=temp_list_name,
                    ctx=ast.Store(),
                    col_offset=ref[1],
                    end_col_offset=ref[2],
                    lineno=ref[3],
                    end_lineno=ref[4],
                )
            ],
            value=ast.List(
                elts=[],
                col_offset=ref[1],
                end_col_offset=ref[2],
                lineno=ref[3],
                end_lineno=ref[4],
            ),
            type_comment=None,
            col_offset=ref[1],
            end_col_offset=ref[2],
            lineno=ref[3],
            end_lineno=ref[4],
        )

        generators = node.generators
        first_gen = generators[-1]
        i = len(generators) - 2

        # Constructs the Python AST for the innermost loop in the list comprehension
        if len(first_gen.ifs) > 0:
            innermost_loop_body = [
                ast.If(
                    test=first_gen.ifs[0],
                    body=[
                        ast.Expr(
                            value=ast.Call(
                                func=ast.Attribute(
                                    value=ast.Name(
                                        id=temp_list_name,
                                        ctx=ast.Load(),
                                        col_offset=ref[1],
                                        end_col_offset=ref[2],
                                        lineno=ref[3],
                                        end_lineno=ref[4],
                                    ),
                                    attr="append",
                                    ctx=ast.Load(),
                                    col_offset=ref[1],
                                    end_col_offset=ref[2],
                                    lineno=ref[3],
                                    end_lineno=ref[4],
                                ),
                                args=[node.elt],
                                keywords=[],
                                col_offset=ref[1],
                                end_col_offset=ref[2],
                                lineno=ref[3],
                                end_lineno=ref[4],
                            ),
                            col_offset=ref[1],
                            end_col_offset=ref[2],
                            lineno=ref[3],
                            end_lineno=ref[4],
                        )
                    ],
                    orelse=[],
                    col_offset=ref[1],
                    end_col_offset=ref[2],
                    lineno=ref[3],
                    end_lineno=ref[4],
                )
            ]
        else:
            innermost_loop_body = [
                ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=ast.Name(
                                id=temp_list_name,
                                ctx=ast.Load(),
                                col_offset=ref[1],
                                end_col_offset=ref[2],
                                lineno=ref[3],
                                end_lineno=ref[4],
                            ),
                            attr="append",
                            ctx=ast.Load(),
                            col_offset=ref[1],
                            end_col_offset=ref[2],
                            lineno=ref[3],
                            end_lineno=ref[4],
                        ),
                        args=[node.elt],
                        keywords=[],
                        col_offset=ref[1],
                        end_col_offset=ref[2],
                        lineno=ref[3],
                        end_lineno=ref[4],
                    ),
                    col_offset=ref[1],
                    end_col_offset=ref[2],
                    lineno=ref[3],
                    end_lineno=ref[4],
                )
            ]

        loop_collection = [
            ast.For(
                target=self.identify_piece(
                    first_gen.target, prev_scope_id_dict, curr_scope_id_dict
                ),
                iter=first_gen.iter,
                # iter=self.identify_piece(first_gen.iter, prev_scope_id_dict, curr_scope_id_dict),
                body=innermost_loop_body,
                orelse=[],
                col_offset=ref[1],
                end_col_offset=ref[2],
                lineno=ref[3],
                end_lineno=ref[4],
            )
        ]

        # Every other loop in the list comprehension wraps itself around the previous loop that we
        # added
        while i >= 0:
            curr_gen = generators[i]
            if len(curr_gen.ifs) > 0:
                # TODO: if multiple ifs exist per a single generator then we have to expand this
                curr_if = curr_gen.ifs[0]
                next_loop = ast.For(
                    target=self.identify_piece(
                        curr_gen.target, curr_scope_id_dict, prev_scope_id_dict
                    ),
                    iter=self.identify_piece(
                        curr_gen.iter, curr_scope_id_dict, prev_scope_id_dict
                    ),
                    body=[
                        ast.If(
                            test=curr_if,
                            body=[loop_collection[0]],
                            orelse=[],
                            col_offset=ref[1],
                            end_col_offset=ref[2],
                            lineno=ref[3],
                            end_lineno=ref[4],
                        )
                    ],
                    orelse=[],
                    col_offset=ref[1],
                    end_col_offset=ref[2],
                    lineno=ref[3],
                    end_lineno=ref[4],
                )

            else:
                next_loop = ast.For(
                    target=self.identify_piece(
                        curr_gen.target, curr_scope_id_dict, prev_scope_id_dict
                    ),
                    iter=self.identify_piece(
                        curr_gen.iter, curr_scope_id_dict, prev_scope_id_dict
                    ),
                    body=[loop_collection[0]],
                    orelse=[],
                    col_offset=ref[1],
                    end_col_offset=ref[2],
                    lineno=ref[3],
                    end_lineno=ref[4],
                )

            loop_collection.insert(0, next_loop)
            i = i - 1

        temp_cast = self.visit(
            temp_assign, prev_scope_id_dict, curr_scope_id_dict
        )
        loop_cast = self.visit(
            loop_collection[0], prev_scope_id_dict, curr_scope_id_dict
        )

        to_ret = []
        to_ret.extend(temp_cast)
        to_ret.extend(loop_cast)

        return to_ret

    @visit.register
    def visit_DictComp(
        self,
        node: ast.DictComp,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        ref = [
            self.filenames[-1],
            node.col_offset,
            node.end_col_offset,
            node.lineno,
            node.end_lineno,
        ]

        # node (ast.DictComp)
        #  key       - what makes the keys
        #  value     - what makes the valuedds
        #  generators - list of 'comprehension' nodes

        temp_dict_name = f"dict__temp_"

        generators = node.generators
        first_gen = generators[-1]
        i = len(generators) - 2
        temp_assign = ast.Assign(
            targets=[
                ast.Name(
                    id=temp_dict_name,
                    ctx=ast.Store(),
                    col_offset=ref[1],
                    end_col_offset=ref[2],
                    lineno=ref[3],
                    end_lineno=ref[4],
                )
            ],
            value=ast.Dict(
                keys=[],
                values=[],
                col_offset=ref[1],
                end_col_offset=ref[2],
                lineno=ref[3],
                end_lineno=ref[4],
            ),
            type_comment=None,
            col_offset=ref[1],
            end_col_offset=ref[2],
            lineno=ref[3],
            end_lineno=ref[4],
        )

        # Constructs the Python AST for the innermost loop in the dict comprehension
        if len(first_gen.ifs) > 0:
            innermost_loop_body = ast.If(
                test=first_gen.ifs[0],
                body=[
                    ast.Assign(
                        targets=[
                            ast.Subscript(
                                value=ast.Name(
                                    id=temp_dict_name,
                                    ctx=ast.Load(),
                                    col_offset=ref[1],
                                    end_col_offset=ref[2],
                                    lineno=ref[3],
                                    end_lineno=ref[4],
                                ),
                                slice=node.key,
                                ctx=ast.Store(),
                                col_offset=ref[1],
                                end_col_offset=ref[2],
                                lineno=ref[3],
                                end_lineno=ref[4],
                            )
                        ],
                        value=node.value,
                        type_comment=None,
                        col_offset=ref[1],
                        end_col_offset=ref[2],
                        lineno=ref[3],
                        end_lineno=ref[4],
                    )
                ],
                orelse=[],
                col_offset=ref[1],
                end_col_offset=ref[2],
                lineno=ref[3],
                end_lineno=ref[4],
            )
        else:
            innermost_loop_body = ast.Assign(
                targets=[
                    ast.Subscript(
                        value=ast.Name(
                            id=temp_dict_name,
                            ctx=ast.Load(),
                            col_offset=ref[1],
                            end_col_offset=ref[2],
                            lineno=ref[3],
                            end_lineno=ref[4],
                        ),
                        slice=node.key,
                        ctx=ast.Store(),
                        col_offset=ref[1],
                        end_col_offset=ref[2],
                        lineno=ref[3],
                        end_lineno=ref[4],
                    )
                ],
                value=node.value,
                type_comment=None,
                col_offset=ref[1],
                end_col_offset=ref[2],
                lineno=ref[3],
                end_lineno=ref[4],
            )

        loop_collection = [
            ast.For(
                target=self.identify_piece(
                    first_gen.target, prev_scope_id_dict, curr_scope_id_dict
                ),
                iter=first_gen.iter,
                # iter=self.identify_piece(first_gen.iter, prev_scope_id_dict, curr_scope_id_dict),
                body=[innermost_loop_body],
                orelse=[],
                col_offset=ref[1],
                end_col_offset=ref[2],
                lineno=ref[3],
                end_lineno=ref[4],
            )
        ]

        # Every other loop in the list comprehension wraps itself around the previous loop that we
        # added
        while i >= 0:
            curr_gen = generators[i]
            if len(curr_gen.ifs) > 0:
                # TODO: if multiple ifs exist per a single generator then we have to expand this
                curr_if = curr_gen.ifs[0]
                next_loop = ast.For(
                    target=self.identify_piece(
                        curr_gen.target, prev_scope_id_dict, curr_scope_id_dict
                    ),
                    iter=curr_gen.iter,
                    # iter=self.identify_piece(curr_gen.iter, prev_scope_id_dict, curr_scope_id_dict),
                    body=[
                        ast.If(
                            test=curr_if,
                            body=[loop_collection[0]],
                            orelse=[],
                            col_offset=ref[1],
                            end_col_offset=ref[2],
                            lineno=ref[3],
                            end_lineno=ref[4],
                        )
                    ],
                    orelse=[],
                    col_offset=ref[1],
                    end_col_offset=ref[2],
                    lineno=ref[3],
                    end_lineno=ref[4],
                )
            else:
                next_loop = ast.For(
                    target=self.identify_piece(
                        curr_gen.target, prev_scope_id_dict, curr_scope_id_dict
                    ),
                    iter=curr_gen.iter,
                    # iter=self.identify_piece(curr_gen.iter, prev_scope_id_dict, curr_scope_id_dict),
                    body=[loop_collection[0]],
                    orelse=[],
                    col_offset=ref[1],
                    end_col_offset=ref[2],
                    lineno=ref[3],
                    end_lineno=ref[4],
                )
            loop_collection.insert(0, next_loop)
            i = i - 1

        temp_cast = self.visit(
            temp_assign, prev_scope_id_dict, curr_scope_id_dict
        )
        loop_cast = self.visit(
            loop_collection[0], prev_scope_id_dict, curr_scope_id_dict
        )

        to_ret = []
        to_ret.extend(temp_cast)
        to_ret.extend(loop_cast)

        return to_ret

    @visit.register
    def visit_If(
        self, node: ast.If, prev_scope_id_dict: Dict, curr_scope_id_dict: Dict
    ):
        """Visits a PyAST If node. Which is used to represent If statements.
        We visit each of the pieces accordingly and construct the CAST
        representation. else/elif statements are stored in the 'orelse' field,
        if there are any.

        Args:
            node (ast.If): A PyAST If node.

        Returns:
            ModelIf: A CAST If statement node.
        """

        node_test = self.visit(
            node.test, prev_scope_id_dict, curr_scope_id_dict
        )

        node_body = []
        if len(node.body) > 0:
            for piece in node.body:
                node_body.extend(
                    self.visit(piece, prev_scope_id_dict, curr_scope_id_dict)
                )

        node_orelse = []
        if len(node.orelse) > 0:
            for piece in node.orelse:
                node_orelse.extend(
                    self.visit(piece, prev_scope_id_dict, curr_scope_id_dict)
                )

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        return [ModelIf(node_test[0], node_body, node_orelse, source_refs=ref)]

    @visit.register
    def visit_Global(
        self,
        node: ast.Global,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Global node.
        What this does is write in the IDs for variables that are
        explicitly declared as global within a scope using the global keyword
        as follows
        global x [, y, z, etc..]

        Args:
            node (ast.Global): A PyAST Global node
            prev_scope_id_dict (Dict): Dictionary containing the scope's current variable : ID maps

        Returns:
            List: empty list
        """

        for v in node.names:
            unique_name = construct_unique_name(self.filenames[-1], v)
            curr_scope_id_dict[unique_name] = self.global_identifier_dict[
                unique_name
            ]
        return []

    @visit.register
    def visit_IfExp(
        self,
        node: ast.IfExp,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST IfExp node, which is Python's ternary operator.
        The node gets translated into a CAST ModelIf node by visiting all its parts,
        since IfExp behaves like an If statement.

        # TODO: Rethink how this is done to better reflect
         - ternary for assignments
         - ternary in function call arguments

        # NOTE: Do we want to treat this as a conditional block in GroMEt? But it shouldn't show up in the expression tree

        Args:
            node (ast.IfExp): [description]
        """

        node_test = self.visit(
            node.test, prev_scope_id_dict, curr_scope_id_dict
        )
        node_body = self.visit(
            node.body, prev_scope_id_dict, curr_scope_id_dict
        )
        node_orelse = self.visit(
            node.orelse, prev_scope_id_dict, curr_scope_id_dict
        )
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        return [ModelIf(node_test[0], node_body, node_orelse, source_refs=ref)]

    @visit.register
    def visit_Import(
        self,
        node: ast.Import,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Import node, which is used for importing libraries
        that are used in programs. In particular, it's imports in the form of
        'import X', where X is some library.

        Args:
            node (ast.Import): A PyAST Import node

        Returns:
        """
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        names = node.names
        to_ret = []
        for alias in names:
            as_name = alias.asname
            orig_name = alias.name

            # Construct the path of the module, relative to where we are at
            # TODO: (Still have to handle things like '..')
            name = alias.name

            # module1.x, module2.x
            # {module1: {x: 1}, module2: {x: 4}}

            # For cases like 'import module as something_else'
            # We note the alias that the import uses for this module
            # Qualify names
            if as_name is not None:
                self.aliases[as_name] = orig_name
                name = alias.asname

            # TODO: Could use a flag to mark a Module as an import (old)
            if orig_name in BUILTINS or find_std_lib_module(orig_name):
                self.insert_next_id(self.global_identifier_dict, name)
                to_ret.append(
                    ModelImport(
                        name=orig_name,
                        alias=as_name,
                        symbol=None,
                        all=False,
                        source_refs=ref,
                    )
                )
        return to_ret

    @visit.register
    def visit_ImportFrom(
        self,
        node: ast.ImportFrom,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST ImportFrom node, which is used for importing libraries
        that are used in programs. In particular, it's imports in the form of
        'import X', where X is some library.

        Args:
            node (ast.Import): A PyAST Import node

        Returns:
        """

        # Construct the path of the module, relative to where we are at
        # (TODO: Still have to handle things like '..')
        # TODO: What about importing individual functions from a module M
        #        that call other functions from that same module M
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        name = node.module
        if name in self.aliases:
            name = self.aliases[name]

        aliases = node.names
        to_ret = []
        for (
            alias
        ) in (
            aliases
        ):  # Iterate through the symbols that are being imported and create individual imports for each
            if alias.asname is not None:
                self.aliases[alias.asname] = alias.name

            if name in BUILTINS or find_std_lib_module(name):
                if alias.name == "*":
                    to_ret.append(
                        ModelImport(
                            name=name,
                            alias=None,
                            symbol=None,
                            all=True,
                            source_refs=ref,
                        )
                    )
                else:
                    to_ret.append(
                        ModelImport(
                            name=name,
                            alias=None,
                            symbol=alias.name,
                            all=False,
                            source_refs=ref,
                        )
                    )
            else:  # User defined module import
                if alias.name == "*":
                    to_ret.append(
                        ModelImport(
                            name=name,
                            alias=None,
                            symbol=None,
                            all=True,
                            source_refs=ref,
                        )
                    )
                else:
                    to_ret.append(
                        ModelImport(
                            name=name,
                            alias=None,
                            symbol=alias.name,
                            all=False,
                            source_refs=ref,
                        )
                    )

        return to_ret

    @visit.register
    def visit_List(
        self,
        node: ast.List,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST List node. Which is used to represent Python lists.

        Args:
            node (ast.List): A PyAST List node.

        Returns:
            List: A CAST List node.
        """

        source_code_data_type = ["Python", "3.8", "List"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        # TODO: How to handle constructors with variables?
        if len(node.elts) > 0:
            to_ret = []
            for piece in node.elts:
                to_ret.extend(
                    self.visit(piece, prev_scope_id_dict, curr_scope_id_dict)
                )
            # TODO: How to represent computations like '[0.0] * 1000' in some kind of type constructing system
            # and then how could we store that in these LiteralValue nodes?
            return [
                LiteralValue(
                    StructureType.LIST, to_ret, source_code_data_type, ref
                )
            ]
            # return [List(to_ret,source_refs=ref)]
        else:
            return [
                LiteralValue(
                    StructureType.LIST, [], source_code_data_type, ref
                )
            ]
            # return [List([],source_refs=ref)]

    @visit.register
    def visit_Module(
        self,
        node: ast.Module,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Module node. This is the starting point of CAST Generation,
        as the body of the Module node (usually) contains the entire Python
        program.

        Args:
            node (ast.Module): A PyAST Module node.

        Returns:
            Module: A CAST Module node.
        """

        # Visit all the nodes and make a Module object out of them
        body = []
        funcs = []
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=-1,
                col_end=-1,
                row_start=-1,
                row_end=-1,
            )
        ]
        self.module_stack.append(node)
        for piece in node.body:
            # Defer visiting function defs until all global vars are processed
            if isinstance(piece, ast.FunctionDef):
                unique_name = construct_unique_name(
                    self.filenames[-1], piece.name
                )
                self.insert_next_id(curr_scope_id_dict, unique_name)
                prev_scope_id_dict[unique_name] = curr_scope_id_dict[
                    unique_name
                ]
                funcs.append(piece)
                continue

            to_add = self.visit(piece, prev_scope_id_dict, curr_scope_id_dict)

            # Global variables (which come about from assignments at the module level)
            # need to have their identifier names set correctly so they can be
            # accessed appropriately later on
            # We check if we just visited an assign and fix its key/value pair in the dictionary
            # So instead of
            #   "var_name" -> ID
            # It becomes
            #   "module_name.var_name" -> ID
            # in the dictionary
            # If an assign happens at the global level, then we must also make sure to
            # Update the global dictionary at this time so that the IDs are defined
            # and are correct
            if isinstance(piece, ast.Assign):
                names = get_node_name(to_add[0])

                # print(piece.lineno)
                for var_name in names:
                    temp_id = curr_scope_id_dict[var_name]
                    del curr_scope_id_dict[var_name]
                    unique_name = construct_unique_name(
                        self.filenames[-1], var_name
                    )
                    curr_scope_id_dict[unique_name] = temp_id
                    merge_dicts(
                        curr_scope_id_dict, self.global_identifier_dict
                    )

            if isinstance(to_add, Module):
                body.extend([to_add])
            else:
                body.extend(to_add)

        merge_dicts(curr_scope_id_dict, self.global_identifier_dict)
        merge_dicts(prev_scope_id_dict, curr_scope_id_dict)

        # Visit all the functions
        for piece in funcs:
            to_add = self.visit(piece, curr_scope_id_dict, {})
            body.extend(to_add)

        self.module_stack.pop()
        return Module(
            name=self.filenames[-1].split(".")[0], body=body, source_refs=ref
        )

    @visit.register
    def visit_Name(
        self,
        node: ast.Name,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """This visits PyAST Name nodes, which consist of
           id: The name of a variable as a string
           ctx: The context in which the variable is being used

        Args:
            node (ast.Name): A PyAST Name node

        Returns:
            Expr: A CAST Expression node

        """
        # TODO: Typing so it's not hardcoded to floats
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        if isinstance(node.ctx, ast.Load):
            if node.id in self.aliases:
                return [Name(self.aliases[node.id], id=-1, source_refs=ref)]

            if node.id not in curr_scope_id_dict:
                if node.id in prev_scope_id_dict:
                    curr_scope_id_dict[node.id] = prev_scope_id_dict[node.id]
                else:
                    unique_name = construct_unique_name(
                        self.filenames[-1], node.id
                    )

                    # We can have the very odd case where a variable is used in a function before
                    # it even exists. To my knowledge this happens in one scenario:
                    # - A global variable, call it z, is used in a function
                    # - Before that function is called in Python code, that global variable
                    #   z is set by another module/another piece of code as a global
                    #   (i.e. by doing module_name.z = a value)
                    # It's not something that is very common (or good) to do, but regardless
                    # we'll catch it here just in case.
                    if unique_name not in self.global_identifier_dict.keys():
                        self.insert_next_id(
                            self.global_identifier_dict, unique_name
                        )

                    curr_scope_id_dict[node.id] = self.global_identifier_dict[
                        unique_name
                    ]

            return [
                Name(node.id, id=curr_scope_id_dict[node.id], source_refs=ref)
            ]

        if isinstance(node.ctx, ast.Store):
            if node.id in self.aliases:
                return [
                    Var(
                        Name(self.aliases[node.id], id=-1, source_refs=ref),
                        "float",
                        source_refs=ref,
                    )
                ]

            if node.id not in curr_scope_id_dict:
                # We construct the unique name for the case that
                # An assignment to a global happens in the global scope
                # (i.e. a loop at the global level)
                # Check if it's in the previous scope not as a global (general case when in a function)
                # then check if it's in the previous scope as a global (when we're at the global scope)
                unique_name = construct_unique_name(
                    self.filenames[-1], node.id
                )
                if node.id in prev_scope_id_dict:
                    curr_scope_id_dict[node.id] = prev_scope_id_dict[node.id]
                elif unique_name in prev_scope_id_dict:
                    curr_scope_id_dict[node.id] = prev_scope_id_dict[
                        unique_name
                    ]
                else:
                    self.insert_next_id(curr_scope_id_dict, node.id)

            return [
                Var(
                    Name(
                        node.id,
                        id=curr_scope_id_dict[node.id],
                        source_refs=ref,
                    ),
                    "float",
                    source_refs=ref,
                )
            ]

        if isinstance(node.ctx, ast.Del):
            # TODO: At some point..
            raise NotImplementedError()

    @visit.register
    def visit_Pass(
        self,
        node: ast.Pass,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """A PyAST Pass visitor, for essentially NOPs."""
        source_code_data_type = ["Python", "3.8", "List"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        return [
            LiteralValue(
                StructureType.LIST,
                "NotImplemented",
                source_code_data_type,
                ref,
            )
        ]

    @visit.register
    def visit_Raise(
        self,
        node: ast.Raise,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """A PyAST Raise visitor, for Raising exceptions

        TODO: To be implemented.
        """
        source_code_data_type = ["Python", "3.8", "List"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        exc_name = ""
        if isinstance(node.exc, ast.Name):
            exc_name = node.exc.id
        elif isinstance(node.exc, ast.Call):
            if isinstance(node.exc.func, ast.Name):
                exc_name = node.exc.func.id

        raise_id = -1
        if "raise" not in self.global_identifier_dict.keys():
            raise_id = self.insert_next_id(
                self.global_identifier_dict, "raise"
            )
        else:
            raise_id = self.global_identifier_dict["raise"]

        return [
            Call(
                Name("raise", raise_id, source_refs=ref),
                [
                    LiteralValue(
                        StructureType.LIST,
                        exc_name,
                        source_code_data_type,
                        ref,
                    )
                ],
                source_refs=ref,
            )
        ]

    @visit.register
    def visit_Return(
        self,
        node: ast.Return,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Return node and creates a CAST return node
           that has one field, which is the expression computing the value
           to be returned. The PyAST's value node is visited.
           The CAST node is then returned.

        Args:
            node (ast.Return): A PyAST Return node

        Returns:
            ModelReturn: A CAST Return node
        """

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        if node.value != None:
            return [
                ModelReturn(
                    self.visit(
                        node.value, prev_scope_id_dict, curr_scope_id_dict
                    )[0],
                    source_refs=ref,
                )
            ]
        else:
            source_code_data_type = ["Python", "3.8", str(type(node.value))]
            val = LiteralValue(None, None, source_code_data_type, ref)
            return [ModelReturn(val, source_refs=ref)]

    @visit.register
    def visit_UnaryOp(
        self,
        node: ast.UnaryOp,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST UnaryOp node. Which represents Python unary operations.
        A dictionary is used to index into which operation we're doing.

        Args:
            node (ast.UnaryOp): A PyAST UnaryOp node.

        Returns:
            UnaryOp: A CAST UnaryOp node.
        """

        ops = {
            ast.UAdd: UnaryOperator.UADD,
            ast.USub: UnaryOperator.USUB,
            ast.Not: UnaryOperator.NOT,
            ast.Invert: UnaryOperator.INVERT,
        }
        op = ops[type(node.op)]
        operand = node.operand

        opd = self.visit(operand, prev_scope_id_dict, curr_scope_id_dict)

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        return [UnaryOp(op, opd[0], source_refs=ref)]

    @visit.register
    def visit_Set(
        self, node: ast.Set, prev_scope_id_dict: Dict, curr_scope_id_dict: Dict
    ):
        """Visits a PyAST Set node. Which is used to represent Python sets.

        Args:
            node (ast.Set): A PyAST Set node.

        Returns:
            Set: A CAST Set node.
        """

        source_code_data_type = ["Python", "3.8", "List"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        if len(node.elts) > 0:
            to_ret = []
            for piece in node.elts:
                to_ret.extend(
                    self.visit(piece, prev_scope_id_dict, curr_scope_id_dict)
                )
            return [
                LiteralValue(
                    StructureType.SET, to_ret, source_code_data_type, ref
                )
            ]
        else:
            return [
                LiteralValue(
                    StructureType.SET, to_ret, source_code_data_type, ref
                )
            ]

    @visit.register
    def visit_Subscript(
        self,
        node: ast.Subscript,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Subscript node, which represents subscripting into
        a list in Python. A Subscript is either a Slice (i.e. x[0:2]), an
        Extended slice (i.e. x[0:2, 3]), or a constant (i.e. x[3]).
        In the Slice case, a loop is generated that fetches the correct elements and puts them
        into a list.
        In the Extended slice case, nested loops are generated as needed to create a final
        result list with the selected elements.
        In the constant case, we can visit and generate a CAST Subscript in a normal way.

        Args:
            node (ast.Subscript): A PyAST Subscript node

        Returns:
            Subscript: A CAST Subscript node
        """

        # value = self.visit(node.value, prev_scope_id_dict, curr_scope_id_dict)[0]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]

        # 'Visit' the slice
        slc = node.slice
        temp_var = f"generated_index_{self.var_count}"
        self.var_count += 1

        if isinstance(slc, ast.Slice):
            if slc.lower is not None:
                start = self.visit(
                    slc.lower, prev_scope_id_dict, curr_scope_id_dict
                )[0]
            else:
                start = LiteralValue(
                    value_type=ScalarType.INTEGER,
                    value=0,
                    source_code_data_type=["Python", "3.8", "Float"],
                    source_refs=ref,
                )

            if slc.upper is not None:
                stop = self.visit(
                    slc.upper, prev_scope_id_dict, curr_scope_id_dict
                )[0]
            else:
                if isinstance(node.value, ast.Call):
                    if isinstance(node.value.func, ast.Attribute):
                        stop = Call(
                            Name("len", source_refs=ref),
                            [Name(node.value.func.attr, source_refs=ref)],
                            source_refs=ref,
                        )
                    else:
                        stop = Call(
                            Name("len", source_refs=ref),
                            [Name(node.value.func.id, source_refs=ref)],
                            source_refs=ref,
                        )
                elif isinstance(node.value, ast.Attribute):
                    stop = Call(
                        Name("len", source_refs=ref),
                        [Name(node.value.attr, source_refs=ref)],
                        source_refs=ref,
                    )
                else:
                    if isinstance(node.value, ast.Subscript):
                        id = self.visit(
                            node.value, prev_scope_id_dict, curr_scope_id_dict
                        )
                    else:
                        id = node.value.id
                    stop = Call(
                        Name("len", source_refs=ref),
                        [Name(id, source_refs=ref)],
                        source_refs=ref,
                    )

            if slc.step is not None:
                step = self.visit(
                    slc.step, prev_scope_id_dict, curr_scope_id_dict
                )[0]
            else:
                step = LiteralValue(
                    value_type=ScalarType.INTEGER,
                    value=1,
                    source_code_data_type=["Python", "3.8", "Float"],
                    source_refs=ref,
                )

            unique_name = construct_unique_name(self.filenames[-1], "slice")
            if unique_name not in prev_scope_id_dict.keys():
                # If a built-in is called, then it gets added to the global dictionary if
                # it hasn't been called before. This is to maintain one consistent ID per built-in
                # function
                if unique_name not in self.global_identifier_dict.keys():
                    self.insert_next_id(
                        self.global_identifier_dict, unique_name
                    )

                prev_scope_id_dict[unique_name] = self.global_identifier_dict[
                    unique_name
                ]

            slice_call = Call(
                func=Name(
                    "slice",
                    id=prev_scope_id_dict[unique_name],
                    source_refs=ref,
                ),
                arguments=[start, stop, step],
                source_refs=ref,
            )

            val = self.visit(
                node.value, prev_scope_id_dict, curr_scope_id_dict
            )[0]

            unique_name = construct_unique_name(self.filenames[-1], "_get")
            if unique_name not in prev_scope_id_dict.keys():
                # If a built-in is called, then it gets added to the global dictionary if
                # it hasn't been called before. This is to maintain one consistent ID per built-in
                # function
                if unique_name not in self.global_identifier_dict.keys():
                    self.insert_next_id(
                        self.global_identifier_dict, unique_name
                    )

                prev_scope_id_dict[unique_name] = self.global_identifier_dict[
                    unique_name
                ]
            # return[Call(Name("Concatenate", id=prev_scope_id_dict[unique_name], source_refs=ref), str_pieces, source_refs=ref)]

            get_call = Call(
                func=Name(
                    "_get", id=prev_scope_id_dict[unique_name], source_refs=ref
                ),
                arguments=[val, slice_call],
                source_refs=ref,
            )

            return [get_call]
        elif isinstance(slc, ast.Index):

            val = self.visit(
                node.value, prev_scope_id_dict, curr_scope_id_dict
            )[0]
            slice_val = self.visit(
                slc.value, prev_scope_id_dict, curr_scope_id_dict
            )[0]
            unique_name = construct_unique_name(self.filenames[-1], "_get")
            if unique_name not in prev_scope_id_dict.keys():
                # If a built-in is called, then it gets added to the global dictionary if
                # it hasn't been called before. This is to maintain one consistent ID per built-in
                # function
                if unique_name not in self.global_identifier_dict.keys():
                    self.insert_next_id(
                        self.global_identifier_dict, unique_name
                    )

                prev_scope_id_dict[unique_name] = self.global_identifier_dict[
                    unique_name
                ]
            get_call = Call(
                func=Name(
                    "_get", id=prev_scope_id_dict[unique_name], source_refs=ref
                ),
                arguments=[val, slice_val],
                source_refs=ref,
            )
            return [get_call]
        elif isinstance(slc, ast.ExtSlice):
            dims = slc.dims
            result = []
            source_code_data_type = ["Python", "3.8", "List"]
            ref = [
                SourceRef(
                    source_file_name=self.filenames[-1],
                    col_start=node.col_offset,
                    col_end=node.end_col_offset,
                    row_start=node.lineno,
                    row_end=node.end_lineno,
                )
            ]
            return [
                LiteralValue(
                    StructureType.LIST,
                    "NotImplemented",
                    source_code_data_type,
                    ref,
                )
            ]

        """
        if isinstance(slc,ast.Slice):
            if slc.lower is not None:
                lower = self.visit(slc.lower, prev_scope_id_dict, curr_scope_id_dict)[0]
            else:
                lower = LiteralValue(value_type=ScalarType.INTEGER, value=0, source_code_data_type=["Python","3.8","Float"], source_refs=ref)

            if slc.upper is not None:
                upper = self.visit(slc.upper, prev_scope_id_dict, curr_scope_id_dict)[0]
            else:
                if isinstance(node.value,ast.Call):
                    if isinstance(node.value.func,ast.Attribute):
                        upper = Call(Name("len", source_refs=ref), [Name(node.value.func.attr, source_refs=ref)], source_refs=ref)
                    else:
                        upper = Call(Name("len", source_refs=ref), [Name(node.value.func.id, source_refs=ref)], source_refs=ref)
                elif isinstance(node.value,ast.Attribute):
                    upper = Call(Name("len", source_refs=ref), [Name(node.value.attr, source_refs=ref)], source_refs=ref)
                else:
                    if isinstance(node.value, ast.Subscript):
                        id = self.visit(node.value, prev_scope_id_dict, curr_scope_id_dict)
                    else:
                        id = node.value.id
                    upper = Call(Name("len", source_refs=ref), [Name(id, source_refs=ref)], source_refs=ref)

            if slc.step is not None:
                step = self.visit(slc.step, prev_scope_id_dict, curr_scope_id_dict)[0]
            else:
                step = LiteralValue(value_type=ScalarType.INTEGER, value=1, source_code_data_type=["Python","3.8","Float"], source_refs=ref)

            if isinstance(node.value,ast.Call):
                if isinstance(node.value.func,ast.Attribute):
                    temp_list = f"{node.value.func.attr}_generated_{self.var_count}"
                else:
                    temp_list = f"{node.value.func.id}_generated_{self.var_count}"
            elif isinstance(node.value,ast.Attribute):
                temp_list = f"{node.value.attr}_generated_{self.var_count}"
            else:
                if isinstance(node.value, ast.Subscript):
                    temp_list = f"temp_generated_{self.var_count}"
                else:
                    temp_list = f"{value.name}_generated_{self.var_count}"
            self.var_count += 1

            new_list = Assignment(Var(Name(temp_list, source_refs=ref), "float", source_refs=ref), List([], source_refs=ref), source_refs=ref)
            loop_var = Assignment(Var(Name(temp_var, source_refs=ref), "float", source_refs=ref), lower, source_refs=ref)

            loop_cond = BinaryOp(
                BinaryOperator.LT,
                Name(temp_var, source_refs=ref),
                upper,
                source_refs=ref
            )

            if isinstance(node.value,ast.Call):
                if isinstance(node.value.func,ast.Attribute):
                    body = [Call(func=Attribute(Name(temp_list, source_refs=ref),Name("append", source_refs=ref),source_refs=ref),
                                arguments=[Subscript(Name(node.value.func.attr, source_refs=ref),Name(temp_var, source_refs=ref), source_refs=ref)],
                                source_refs=ref)]
                else:
                    body = [Call(func=Attribute(Name(temp_list, source_refs=ref),Name("append", source_refs=ref),source_refs=ref),
                                arguments=[Subscript(Name(node.value.func.id, source_refs=ref),Name(temp_var, source_refs=ref), source_refs=ref)],
                                source_refs=ref)]
            elif isinstance(node.value,ast.Attribute):
                body = [Call(func=Attribute(Name(temp_list, source_refs=ref),Name("append", source_refs=ref),source_refs=ref),
                            arguments=[Subscript(Name(node.value.attr, source_refs=ref),Name(temp_var, source_refs=ref), source_refs=ref)],
                            source_refs=ref)]
            else:
                if isinstance(node.value, ast.Subscript):
                    body = [Call(func=Attribute(Name(temp_list, source_refs=ref),Name("append", source_refs=ref),source_refs=ref),
                                arguments=[Subscript(Name("TEMP", source_refs=ref),Name(temp_var, source_refs=ref), source_refs=ref)],
                                source_refs=ref)]
                else:
                    body = [Call(func=Attribute(Name(temp_list, source_refs=ref),Name("append", source_refs=ref),source_refs=ref),
                                arguments=[Subscript(Name(node.value.id, source_refs=ref),Name(temp_var, source_refs=ref), source_refs=ref)],
                                source_refs=ref)]

            loop_increment = [Assignment(
                Var(Name(temp_var, source_refs=ref), "float", source_refs=ref),
                BinaryOp(BinaryOperator.ADD, Name(temp_var, source_refs=ref), step, source_refs=ref),
                source_refs=ref
            )]

            slice_loop = Loop(
                expr=loop_cond, body=body + loop_increment, source_refs=ref
            )

            slice_var = Var(Name(temp_list, source_refs=ref), "float", source_refs=ref)

            return [new_list, loop_var, slice_loop, slice_var]
        elif isinstance(slc,ast.ExtSlice):
            dims = slc.dims
            result = []
            source_code_data_type = ["Python","3.8","List"]
            ref = [SourceRef(source_file_name=self.filenames[-1], col_start=node.col_offset, col_end=node.end_col_offset, row_start=node.lineno, row_end=node.end_lineno)]
            return [LiteralValue(StructureType.LIST, "NotImplemented", source_code_data_type, ref)]

            if isinstance(node.value,ast.Call):
                if isinstance(node.value.func,ast.Attribute):
                    lists = [node.value.func.attr]
                else:
                    lists = [node.value.func.id]
            elif isinstance(node.value,ast.Attribute):
                lists = [node.value.attr]
            else:
                lists = [node.value.id]

            temp_count = 1
            for dim in dims:
                temp_list_name = f"{lists[0]}_{str(temp_count)}"
                temp_count += 1

                list_var = Assignment(Var(Name(temp_list_name, source_refs=ref), "float", source_refs=ref), List([], source_refs=ref), source_refs=ref)
                loop_var = Assignment(Var(Name(temp_var, source_refs=ref), "float", source_refs=ref), Number(0, source_refs=ref),source_refs=ref)

                if isinstance(dim,ast.Slice):
                    # For each dim in dimensions
                    # Check if it's a slice or a constant
                    # For a slice
                    # Take the current temp list
                    # Make a new temp list
                    # Using the slice bounds, generate a loop that assigns the elements of
                    # the slice bounds to the new temp list
                    # Append the cast and temp list accordingly
                    if dim.lower is not None:
                        lower = self.visit(dim.lower, prev_scope_id_dict, curr_scope_id_dict)[0]
                    else:
                        lower = Number(0, source_refs=ref)

                    if dim.upper is not None:
                        upper = self.visit(dim.upper, prev_scope_id_dict, curr_scope_id_dict)[0]
                    else:
                        if isinstance(node.value,ast.Call):
                            if isinstance(node.value.func,ast.Attribute):
                                upper = Call(Name("len", source_refs=ref), [Name(node.value.func.attr, source_refs=ref)], source_refs=ref)
                            else:
                                upper = Call(Name("len", source_refs=ref), [Name(node.value.func.id, source_refs=ref)], source_refs=ref)
                        elif isinstance(node.value,ast.Attribute):
                            upper = Call(Name("len", source_refs=ref), [Name(node.value.attr, source_refs=ref)], source_refs=ref)
                        else:
                            upper = Call(Name("len", source_refs=ref), [Name(node.value.id, source_refs=ref)], source_refs=ref)

                    if dim.step is not None:
                        step = self.visit(dim.step, prev_scope_id_dict, curr_scope_id_dict)[0]
                    else:
                        step = Number(1, source_refs=ref)


                    loop_cond = BinaryOp(
                        BinaryOperator.LT,
                        Name(temp_var, source_refs=ref),
                        upper,
                        source_refs=ref
                    )

                    body = [Call(Attribute(Name(temp_list_name,source_refs=ref),Name("append", source_refs=ref),source_refs=ref),
                                [Subscript(Name(lists[-1], source_refs=ref),Name(temp_var, source_refs=ref),source_refs=ref)],source_refs=ref)]

                    loop_increment = [Assignment(
                        Var(Name(temp_var, source_refs=ref), "float", source_refs=ref),
                        BinaryOp(BinaryOperator.ADD, Name(temp_var, source_refs=ref), step, source_refs=ref),
                        source_refs=ref
                    )]

                    slice_loop = Loop(
                        expr=loop_cond, body=body + loop_increment, source_refs=ref
                    )

                    lists.append(temp_list_name)

                    result.extend([list_var,loop_var,slice_loop])
                if isinstance(dim,ast.Index):
                    # For an index
                    # Take the current temp list
                    # Make a new temp list
                    # This new temp list indexes into the current temp list
                    # and copies the elements according to the index number
                    # Append that new temp list and its corresponding CAST
                    # to our result
                    curr_dim = self.visit(dim, prev_scope_id_dict, curr_scope_id_dict)[0]

                    loop_cond = BinaryOp(
                        BinaryOperator.LT,
                        Name(temp_var, source_refs=ref),
                        Call(Name("len", source_refs=ref), [Name(lists[-1], source_refs=ref)], source_refs=ref),
                        source_refs=ref
                    )

                    body = [Call(Attribute(Name(temp_list_name, source_refs=ref),Name("append", source_refs=ref), source_refs=ref),
                                [Subscript(Name(lists[-1], source_refs=ref), curr_dim, source_refs=ref)], source_refs=ref)]


                    loop_increment = [Assignment(
                        Var(Name(temp_var, source_refs=ref), "float", source_refs=ref),
                        BinaryOp(BinaryOperator.ADD, Name(temp_var, source_refs=ref), Number(1, source_refs=ref), source_refs=ref),
                        source_refs=ref
                    )]

                    slice_loop = Loop(
                        expr=loop_cond, body=body + loop_increment, source_refs=ref
                    )

                    lists.append(temp_list_name)

                    result.extend([list_var,loop_var,slice_loop])

            return result
            """
        # else:
        #   sl = self.visit(slc, prev_scope_id_dict, curr_scope_id_dict)

    @visit.register
    def visit_Index(
        self,
        node: ast.Index,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Index node, which represents the value being used
        for an index. This visitor doesn't create its own CAST node, but
        returns CAST depending on the value that the Index node holds.

        Args:
            node (ast.Index): A CAST Index node.

        Returns:
            AstNode: Depending on what the value of the Index node is,
                     different CAST nodes are returned.
        """

        return self.visit(node.value, prev_scope_id_dict, curr_scope_id_dict)

    @visit.register
    def visit_Tuple(
        self,
        node: ast.Tuple,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST Tuple node. Which is used to represent Python tuple.

        Args:
            node (ast.Tuple): A PyAST Tuple node.

        Returns:
            Set: A CAST Tuple node.
        """

        # source_code_data_type = ["Python","3.8","List"]
        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        # if len(node.elts) > 0:
        to_ret = []
        for piece in node.elts:
            to_ret.extend(
                self.visit(piece, prev_scope_id_dict, curr_scope_id_dict)
            )
        return [Tuple(to_ret, source_refs=ref)]
        # else:
        #   return [LiteralValue(StructureType.TUPLE, [], source_code_data_type, source_refs=ref)]

    @visit.register
    def visit_Try(
        self, node: ast.Try, prev_scope_id_dict: Dict, curr_scope_id_dict: Dict
    ):
        """Visits a PyAST Try node, which represents Try/Except blocks.
        These are used for Python's exception handling

        Currently, the visitor just bypasses the Try/Except feature and just
        generates CAST for the body of the 'Try' block, assuming the exception(s)
        are never thrown.
        """

        body = []
        for piece in node.body:
            body.extend(
                self.visit(piece, prev_scope_id_dict, curr_scope_id_dict)
            )

        return body

    @visit.register
    def visit_While(
        self,
        node: ast.While,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST While node, which represents a while loop.

        Args:
            node (ast.While): a PyAST while node

        Returns:
            Loop: A CAST loop node, which generically represents both For
                  loops and While loops.
        """

        test = self.visit(node.test, prev_scope_id_dict, curr_scope_id_dict)[0]

        # Loops have their own enclosing scopes
        curr_scope_copy = copy.deepcopy(curr_scope_id_dict)
        merge_dicts(prev_scope_id_dict, curr_scope_id_dict)
        loop_body_scope = {}
        body = []
        for piece in node.body + node.orelse:
            to_add = self.visit(piece, curr_scope_id_dict, loop_body_scope)
            body.extend(to_add)

        curr_scope_id_dict = copy.deepcopy(curr_scope_copy)

        ref = [
            SourceRef(
                source_file_name=self.filenames[-1],
                col_start=node.col_offset,
                col_end=node.end_col_offset,
                row_start=node.lineno,
                row_end=node.end_lineno,
            )
        ]
        # loop_body_fn_def = FunctionDef(name="while_temp", func_args=None, body=body)
        # return [Loop(init=[], expr=test, body=loop_body_fn_def, source_refs=ref)]
        return [Loop(init=[], expr=test, body=body, source_refs=ref)]

    @visit.register
    def visit_With(
        self,
        node: ast.With,
        prev_scope_id_dict: Dict,
        curr_scope_id_dict: Dict,
    ):
        """Visits a PyAST With node. With nodes are used as follows:
        with a as b, c as d:
            do things with b and d
        To use aliases on variables and operate on them
        This visitor unrolls the With block and generates the appropriate cast for the
        underlying operations

        Args:
            node (ast.With): a PyAST with node

        Args:
            [AstNode]: A list of CAST nodes, representing whatever operations were happening in the With
                       block before they got unrolled

        """

        ref = None
        variables = []
        for item in node.items:
            ref = [
                SourceRef(
                    source_file_name=self.filenames[-1],
                    col_start=node.col_offset,
                    col_end=node.end_col_offset,
                    row_start=node.lineno,
                    row_end=node.end_lineno,
                )
            ]
            if item.optional_vars != None:
                l = self.visit(
                    item.optional_vars, prev_scope_id_dict, curr_scope_id_dict
                )
                r = self.visit(
                    item.context_expr, prev_scope_id_dict, curr_scope_id_dict
                )
                variables.extend(
                    [Assignment(left=l[0], right=r[0], source_refs=ref)]
                )
            else:
                variables.extend(
                    [
                        self.visit(
                            item.context_expr,
                            prev_scope_id_dict,
                            curr_scope_id_dict,
                        )[0]
                    ]
                )

        body = []
        for piece in node.body:
            body.extend(
                self.visit(piece, prev_scope_id_dict, curr_scope_id_dict)
            )

        return variables + body
