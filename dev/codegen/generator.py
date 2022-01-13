#  Copyright 2020 Accenture Global Solutions Limited
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import annotations

import enum

import itertools as it
import re
import typing as tp
import pathlib
import logging
import dataclasses as dc
import functools as fn

import google.protobuf.descriptor_pb2 as pb_desc  # noqa
import google.protobuf.compiler.plugin_pb2 as pb_plugin  # noqa


class LocationContext:

    def __init__(self, src_locations: tp.List[pb_desc.SourceCodeInfo.Location],
                 src_loc_code: int, src_loc_index: int, indent: int):

        self.src_locations = src_locations
        self.src_loc_code = src_loc_code
        self.src_loc_index = src_loc_index
        self.indent = indent

    def for_index(self, index: int) -> LocationContext:

        return LocationContext(self.src_locations, self.src_loc_code, index, self.indent)


class ECodeGeneration(RuntimeError):

    """
    An error occurred in the code generation process
    (This is not part of the ETrac hierarchy as it is a build-time error)
    """

    pass


class TypeClass(enum.Enum):
    ENUM = 1
    MESSAGE = 2
    SERVICE = 3


@dc.dataclass
class TypeInfo:
    typeClass: TypeClass
    enum: pb_desc.EnumDescriptorProto = None
    message: pb_desc.DescriptorProto = None
    service: pb_desc.ServiceDescriptorProto = None


TYPE_INFO_MAP = tp.Dict[str, TypeInfo]


class TracGenerator:
    
    _FieldType = pb_desc.FieldDescriptorProto.Type

    PRIMITIVE_TYPE_MAPPING = dict({

        _FieldType.TYPE_DOUBLE: float,
        _FieldType.TYPE_FLOAT: float,
        _FieldType.TYPE_INT64: int,
        _FieldType.TYPE_UINT64: int,
        _FieldType.TYPE_INT32: int,
        _FieldType.TYPE_FIXED64: int,
        _FieldType.TYPE_FIXED32: int,
        _FieldType.TYPE_BOOL: bool,
        _FieldType.TYPE_STRING: str,
        
        # Group type is deprecated and not supported in proto3
        # _FieldType.TYPE_GROUP
        
        # Do not include a mapping for message type, it will be handle specially
        # _FieldType.TYPE_MESSAGE

        _FieldType.TYPE_BYTES: bytes,  # TODO: Use bytearray?
        
        _FieldType.TYPE_UINT32: int,

        # Do not include a mapping for enum type, it will be handle specially
        # _FieldType.TYPE_ENUM

        _FieldType.TYPE_SFIXED32: int,
        _FieldType.TYPE_SFIXED64: int,
        _FieldType.TYPE_SINT32: int,
        _FieldType.TYPE_SINT64: int
    })

    INDENT_TEMPLATE = ' ' * 4

    PACKAGE_IMPORT_TEMPLATE = 'from .{MODULE_NAME} import {SYMBOL}\n'

    FILE_TEMPLATE = (
        '{FILE_HEADER}'
        '{STD_IMPORTS}'
        '{PKG_IMPORTS}'
        '{ENUMS_CODE}'
        '\n'
        '{MESSAGES_CODE}'
        '\n'
        '{SERVICES_CODE}'
        '\n')

    FILE_HEADER = (
        '# Code generated by TRAC\n\n')

    STD_IMPORTS = (
        "from __future__ import annotations\n"
        "import typing as _tp  # noqa\n"
        "import dataclasses as _dc  # noqa\n"
        "import enum as _enum  # noqa\n\n")

    MODULE_IMPORT = (
        'import {MODULE}\n')

    MODULE_ALIAS_IMPORT = (
        'import {MODULE} as {ALIAS}\n')

    SCOPE_IMPORT = (
        'from .{MODULE} import *  # noqa\n')

    ENUM_TEMPLATE = (
        '{INDENT}class {CLASS_NAME}(_enum.Enum):'
        '\n\n'
        '{DOC_COMMENT}'
        '{ENUM_VALUES}')

    ENUM_VALUE_TEMPLATE = (
        '{INDENT}{ENUM_VALUE_NAME} = {ENUM_VALUE_NUMBER}, {QUOTED_COMMENT}\n\n')

    DATA_CLASS_TEMPLATE = (
        '{INDENT}@_dc.dataclass\n'
        '{INDENT}class {CLASS_NAME}:\n'
        '\n'
        '{DOC_COMMENT}'
        '{NESTED_ENUMS}'
        '{NESTED_CLASSES}'
        '{DATA_MEMBERS}')

    DATA_MEMBER_TEMPLATE = (
        '{INDENT}{MEMBER_NAME}: {MEMBER_TYPE} = {MEMBER_DEFAULT}'
        '\n\n'
        '{DOC_COMMENT}')

    SERVICE_CLASS_TEMPLATE = (
        '{INDENT}class {SERVICE_NAME}:\n'
        '\n'
        '{DOC_COMMENT}'
        '{METHODS}')

    SERVICE_METHOD_TEMPLATE = (
        '{INDENT}def {METHOD_NAME}(self, request: {REQUEST_TYPE}) -> {RESPONSE_TYPE}:\n'
        '\n'
        '{DOC_COMMENT}'
        '{NEXT_INDENT}pass\n\n')

    PASS_TEMPLATE = (
        '{INDENT}pass\n\n')

    COMMENT_SINGLE_LINE = (
        '{INDENT}"""{COMMENT}"""\n\n')

    COMMENT_MULTI_LINE = (
        '{INDENT}"""\n'
        '{COMMENT}\n'
        '{INDENT}"""\n\n')

    ENUM_COMMENT_SINGLE_LINE = \
        '"""{COMMENT}"""'

    ENUM_COMMENT_MULTI_LINE = \
        '"""{COMMENT}\n' \
        '{INDENT}"""'

    def __init__(self, options: tp.Dict[str, tp.Any] = None):

        logging.basicConfig(level=logging.DEBUG)
        self._log = logging.getLogger(TracGenerator.__name__)
        self._options = options or {}

        self._desc_file_enum = self.get_field_number(pb_desc.FileDescriptorProto, "enum_type")
        self._desc_file_message = self.get_field_number(pb_desc.FileDescriptorProto, "message_type")
        self._desc_file_service = self.get_field_number(pb_desc.FileDescriptorProto, "service")
        self._desc_enum_value = self.get_field_number(pb_desc.EnumDescriptorProto, "value")
        self._desc_message_field = self.get_field_number(pb_desc.DescriptorProto, "field")
        self._desc_service_method = self.get_field_number(pb_desc.ServiceDescriptorProto, "method")

    def build_type_map(self, proto_files: tp.List[pb_desc.FileDescriptorProto]) -> TYPE_INFO_MAP:

        # self._log.info("Building type map...")

        return fn.reduce(self.build_type_map_for_file, proto_files, {})

    def build_type_map_for_file(
            self, types: TYPE_INFO_MAP,
            proto_file: pb_desc.FileDescriptorProto) \
            -> TYPE_INFO_MAP:

        self._log.info(f" [ TYPES ] -> {proto_file.name}")

        scope = proto_file.package + "." if proto_file.package else ""

        local_types = fn.reduce(
            fn.partial(self.build_type_map_for_message, scope),
            proto_file.message_type, types)

        for enum_type in proto_file.enum_type:
            enum_type_name = f"{scope}{enum_type.name}"
            enum_type_info = TypeInfo(TypeClass.ENUM, enum=enum_type)
            local_types[enum_type_name] = enum_type_info

        for service in proto_file.service:
            service_name = f"{scope}{service.name}"
            service_info = TypeInfo(TypeClass.SERVICE, service=service)
            local_types[service_name] = service_info

        return local_types

    def build_type_map_for_message(
            self, scope: str, types: TYPE_INFO_MAP,
            proto_msg: pb_desc.DescriptorProto) \
            -> TYPE_INFO_MAP:

        inner_scope = f"{scope}{proto_msg.name}."

        local_types = fn.reduce(
            fn.partial(self.build_type_map_for_message, inner_scope),
            proto_msg.nested_type, types)

        for enum_type in proto_msg.enum_type:
            enum_type_name = f"{inner_scope}{enum_type.name}"
            enum_type_info = TypeInfo(TypeClass.ENUM, enum=enum_type)
            local_types[enum_type_name] = enum_type_info

        message_type_name = f"{scope}{proto_msg.name}"
        message_type_info = TypeInfo(TypeClass.MESSAGE, message=proto_msg)
        local_types[message_type_name] = message_type_info

        return local_types

    def generate_package(
            self, api_package: str,
            files: tp.List[pb_desc.FileDescriptorProto],
            type_map: TYPE_INFO_MAP) \
            -> tp.List[pb_plugin.CodeGeneratorResponse.File]:

        self._log.info(f" [ PKG   ] -> {api_package} ({len(files)} proto files)")

        flat_pack = "flat_pack" in self._options
        output_files = []

        # Use the protobuf package as the Python package
        package_path = pathlib.Path(*api_package.split("."))
        package_imports = ""

        for file_descriptor in files:

            # Run the generator to produce code for the Python module
            src_locations = file_descriptor.source_code_info.location

            module_code = self.generate_file(
                src_locations, 0, file_descriptor,
                api_package, type_map, not flat_pack)

            if flat_pack and len(files) > 0:

                if len(output_files) == 0:
                    module_code = self.FILE_HEADER + self.STD_IMPORTS + module_code
                    module_path = package_path.with_suffix(".py")
                else:
                    # Setting module path = "" will append module_code to the previous file
                    module_path = ""

            else:
                # Strip multiple newlines from the end of the file
                while module_code.endswith("\n\n"):
                    module_code = module_code[:-1]

                # Path is formed from the python package and the module name (.proto file stem)
                proto_file = pathlib.PurePath(file_descriptor.name)
                module_path = package_path.joinpath(proto_file.stem).with_suffix(".py")

                # Generate import statements to include in the package-level __init__ file
                package_imports += self.generate_package_imports(file_descriptor)

            # Create a generator response for the module
            file_response = pb_plugin.CodeGeneratorResponse.File()
            file_response.content = module_code
            file_response.name = str(module_path)

            output_files.append(file_response)

        if not flat_pack or len(files) == 0:

            # Add an extra generator response file for the package-level __init__ file
            package_init_file = pb_plugin.CodeGeneratorResponse.File()
            package_init_file.name = str(package_path.joinpath("__init__.py"))
            package_init_file.content = self.FILE_HEADER[:-1] + package_imports

            output_files.append(package_init_file)

        package_filter = self._options.get("packages")

        if package_filter is None or api_package == package_filter or api_package.startswith(package_filter + "."):
            return output_files

        elif package_filter.startswith(api_package + "."):

            empty_init_file = pb_plugin.CodeGeneratorResponse.File()
            empty_init_file.name = str(package_path.joinpath("__init__.py"))
            empty_init_file.content = ""
            return [empty_init_file]

        else:
            return []

    def generate_package_imports(self, descriptor: pb_desc.FileDescriptorProto) -> str:

        file_path = pathlib.Path(descriptor.name)
        module_name = file_path.stem

        imports = ""

        if len(descriptor.enum_type) > 0 or len(descriptor.message_type) > 0:
            imports += "\n"

        for enum_type in descriptor.enum_type:
            imports += self.PACKAGE_IMPORT_TEMPLATE.format(
                MODULE_NAME=module_name,
                SYMBOL=enum_type.name)

        for message_type in descriptor.message_type:
            imports += self.PACKAGE_IMPORT_TEMPLATE.format(
                MODULE_NAME=module_name,
                SYMBOL=message_type.name)

        return imports

    def generate_file(
            self, src_loc, indent: int,
            descriptor: pb_desc.FileDescriptorProto,
            api_package: str, type_map: TYPE_INFO_MAP,
            include_header: bool = True) -> str:

        self._log.info(f" [ FILE  ] -> {descriptor.name}")

        if include_header:
            file_header = self.FILE_HEADER
            std_imports = self.STD_IMPORTS
            import_stmts = self.generate_module_imports(descriptor, api_package)

        else:
            file_header = ""
            std_imports = ""
            import_stmts = []

        pkg_imports = "".join(import_stmts) + "\n\n" if any(import_stmts) else ""

        # Generate enums
        enums_ctx = self.index_sub_ctx(src_loc, self._desc_file_enum, indent)
        enums = []

        for ctx, desc in zip(enums_ctx, descriptor.enum_type):
            enum_ = self.generate_enum(ctx, desc, enum_scope=None)
            enums.append(enum_)

        # Generate messages
        messages_ctx = self.index_sub_ctx(src_loc, self._desc_file_message, indent)
        messages = []

        for ctx, desc in zip(messages_ctx, descriptor.message_type):
            message = self.generate_data_class(descriptor.package, descriptor.package, ctx, desc, type_map)
            messages.append(message)

        # Generate services
        services_ctx = self.index_sub_ctx(src_loc, self._desc_file_service, indent)
        services = []

        for service_ctx, service_desc in zip(services_ctx, descriptor.service):
            service = self.generate_service_class(descriptor.package, service_ctx, service_desc)
            services.append(service)

        # Populate the template
        code = self.FILE_TEMPLATE \
            .replace("{INDENT}", self.INDENT_TEMPLATE * indent) \
            .replace("{FILE_HEADER}", file_header) \
            .replace("{STD_IMPORTS}", std_imports) \
            .replace("{PKG_IMPORTS}", "".join(pkg_imports)) \
            .replace("{ENUMS_CODE}", "\n".join(enums)) \
            .replace("{MESSAGES_CODE}", "\n".join(messages)) \
            .replace("{SERVICES_CODE}", "\n".join(services))

        return code

    def generate_module_imports(self, descriptor: pb_desc.FileDescriptorProto, api_package: str):

        import_proto_pattern = re.compile(r"^(trac/.+)/([^/]+)\.proto$")

        import_stmts = []
        prior_imports = set()

        # Generate imports
        for import_proto in descriptor.dependency:

            import_match = import_proto_pattern.match(import_proto)

            if import_match:

                import_package = import_match.group(1).replace("/", ".")
                import_module = import_match.group(2).replace("/", ".")

                if import_package == api_package and import_module not in prior_imports:

                    prior_imports.add(import_module)

                    import_stmt = self.SCOPE_IMPORT \
                        .replace("{MODULE}", import_module)

                    import_stmts.append(import_stmt)

                elif import_package not in prior_imports:

                    prior_imports.add(import_package)

                    target_package = self._options["target_package"] \
                        if "target_package" in self._options \
                        else "trac"

                    sub_package = import_package.replace("trac.", "")
                    qualified_package = target_package + "." + sub_package
                    alias = import_package[import_package.rfind(".") + 1:]

                    import_stmt = self.MODULE_ALIAS_IMPORT \
                        .replace("{MODULE}", qualified_package) \
                        .replace("{ALIAS}", alias)

                    import_stmts.append(import_stmt)

        return import_stmts

    def generate_service_class(
            self, package: str, ctx: LocationContext,
            descriptor: pb_desc.ServiceDescriptorProto) -> str:

        log_indent = self.INDENT_TEMPLATE * (ctx.indent + 1)
        self._log.info(f" [ SVC   ] {log_indent}-> {descriptor.name}")

        filtered_loc = self.filter_src_location(ctx.src_locations, ctx.src_loc_code, ctx.src_loc_index)

        # Generate service-level documentation
        raw_comment = self.comment_for_current_location(filtered_loc)
        doc_comment = self.format_doc_comment(ctx, raw_comment, next_indent=True)

        # Generate methods
        methods_ctx = self.index_sub_ctx(filtered_loc, self._desc_service_method, ctx.indent + 1)
        methods = []

        for method_ctx, method_desc in zip(methods_ctx, descriptor.method):
            method = self.generate_service_method(package, method_ctx, method_desc)
            methods.append(method)

        return self.SERVICE_CLASS_TEMPLATE \
            .replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent) \
            .replace("{SERVICE_NAME}", descriptor.name) \
            .replace("{DOC_COMMENT}", doc_comment) \
            .replace("{METHODS}", "".join(methods))

    def generate_service_method(
            self, package: str, ctx: LocationContext,
            descriptor: pb_desc.MethodDescriptorProto) -> str:

        filtered_loc = self.filter_src_location(ctx.src_locations, ctx.src_loc_code, ctx.src_loc_index)

        # Method request/response types
        request_type = self.python_type_name(package, descriptor.input_type, make_relative=True)
        response_type = self.python_type_name(package, descriptor.output_type, make_relative=True)

        # Generate method-level documentation
        raw_comment = self.comment_for_current_location(filtered_loc)
        doc_comment = self.format_doc_comment(ctx, raw_comment, next_indent=True)

        return self.SERVICE_METHOD_TEMPLATE \
            .replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent) \
            .replace("{NEXT_INDENT}", self.INDENT_TEMPLATE * (ctx.indent + 1)) \
            .replace("{METHOD_NAME}", descriptor.name) \
            .replace("{REQUEST_TYPE}", request_type) \
            .replace("{RESPONSE_TYPE}", response_type) \
            .replace("{DOC_COMMENT}", doc_comment)

    def generate_data_class(
            self, package: str, message_scope: str, ctx: LocationContext,
            descriptor: pb_desc.DescriptorProto, types: TYPE_INFO_MAP) -> str:

        log_indent = self.INDENT_TEMPLATE * (ctx.indent + 1)
        self._log.info(f" [ MSG   ] {log_indent}-> {descriptor.name}")

        # Source location and scope for this message
        filtered_loc = self.filter_src_location(ctx.src_locations, ctx.src_loc_code, ctx.src_loc_index)
        nested_scope = descriptor.name if message_scope is None else f"{message_scope}.{descriptor.name}"

        # Generate nested enums
        nested_enums_ctx = self.index_sub_ctx(filtered_loc, self._desc_file_enum, ctx.indent + 1)
        nested_enums = []

        for enum_ctx, enum_desc in zip(nested_enums_ctx, descriptor.enum_type):
            nested_enum = self.generate_enum(enum_ctx, enum_desc, nested_scope)
            nested_enums.append(nested_enum)

        # Generate nested message classes
        nested_types_ctx = self.index_sub_ctx(filtered_loc, self._desc_file_message, ctx.indent + 1)
        nested_types = []

        for sub_ctx, sub_desc in zip(nested_types_ctx, descriptor.nested_type):
            if not sub_desc.options.map_entry:
                nested_type = self.generate_data_class(package, nested_scope, sub_ctx, sub_desc, types)
                nested_types.append(nested_type)

        # Generate data members - these may reference known message and enum types
        data_members_ctx = LocationContext(filtered_loc, ctx.src_loc_code, ctx.src_loc_index, ctx.indent + 1)
        data_members = self.generate_data_members(package, data_members_ctx, descriptor, types)

        # Generate comments
        raw_comment = self.comment_for_current_location(filtered_loc)
        doc_comment = self.format_doc_comment(ctx, raw_comment, next_indent=True)

        return self.DATA_CLASS_TEMPLATE \
            .replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent) \
            .replace("{NEXT_INDENT}", self.INDENT_TEMPLATE * (ctx.indent + 1)) \
            .replace("{CLASS_NAME}", descriptor.name) \
            .replace("{DOC_COMMENT}", doc_comment) \
            .replace("{NESTED_ENUMS}", "".join(nested_enums)) \
            .replace("{NESTED_CLASSES}", "".join(nested_types)) \
            .replace("{DATA_MEMBERS}", data_members)

    def generate_data_members(
            self, package: str, ctx: LocationContext,
            descriptor: pb_desc.DescriptorProto,
            types: TYPE_INFO_MAP) -> str:

        # Generate a pass statement if the class has no members
        if not descriptor.field:
            return self.PASS_TEMPLATE.replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent)

        members_ctx = self.index_sub_ctx(
            ctx.src_locations,
            self._desc_message_field,
            ctx.indent)

        members = list(map(lambda f: self.generate_data_member(
            package, next(members_ctx), descriptor, f, types),
            descriptor.field))

        return "".join(members)

    def generate_data_member(
            self, package: str, ctx: LocationContext,
            message: pb_desc.DescriptorProto,
            field: pb_desc.FieldDescriptorProto,
            types: TYPE_INFO_MAP) \
            -> str:

        filtered_loc = self.filter_src_location(ctx.src_locations, ctx.src_loc_code, ctx.src_loc_index)

        field_type = self.python_field_type(package, field, message)
        field_default = self.python_default_value(package, field, message, types)

        raw_comment = self.comment_for_current_location(filtered_loc)
        doc_comment = self.format_doc_comment(ctx, raw_comment)

        return self.DATA_MEMBER_TEMPLATE \
            .replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent) \
            .replace("{MEMBER_NAME}", field.name) \
            .replace("{MEMBER_TYPE}", field_type) \
            .replace("{MEMBER_DEFAULT}", field_default) \
            .replace("{DOC_COMMENT}", doc_comment)

    def generate_enum(
            self, ctx: LocationContext,
            descriptor: pb_desc.EnumDescriptorProto,
            enum_scope: str = None) \
            -> str:

        log_indent = self.INDENT_TEMPLATE * (ctx.indent + 1)
        self._log.info(f" [ ENUM  ] {log_indent}-> {descriptor.name}")

        # There is a problem constructing Python data classes for nested enums
        # The default initializer is not available until the outer class is declared
        # A solution is to create the enum at file scope with a _ prefix and alias it to create the nested version
        # https://stackoverflow.com/a/54489183
        if enum_scope:
            scoped_name = f"{enum_scope}.{descriptor.name}"
            err = f" [ ENUM  ] {scoped_name}: Nested enums not currently supported"
            self._log.error(err)
            raise ECodeGeneration(err)

        filtered_loc = self.filter_src_location(ctx.src_locations, ctx.src_loc_code, ctx.src_loc_index)

        # Generate a pass statement if the enum has no members (protoc should prevent this anyway)
        if not descriptor.value:
            return self.PASS_TEMPLATE.replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent)

        # Generate enum values
        values_ctx = self.index_sub_ctx(
            filtered_loc,
            self._desc_enum_value,
            ctx.indent + 1)

        values = list(map(lambda ev: self.generate_enum_value(
            next(values_ctx), ev),
            descriptor.value))

        # Generate top level comments for the type
        raw_comment = self.comment_for_current_location(filtered_loc)
        doc_comment = self.format_doc_comment(ctx, raw_comment, next_indent=True)

        # Populate the template
        return self.ENUM_TEMPLATE \
            .replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent) \
            .replace("{NEXT_INDENT}", self.INDENT_TEMPLATE * (ctx.indent + 1)) \
            .replace("{DOC_COMMENT}", doc_comment) \
            .replace("{CLASS_NAME}", descriptor.name) \
            .replace("{ENUM_VALUES}", "".join(values))

    def generate_enum_value(self, ctx: LocationContext, descriptor: pb_desc.EnumValueDescriptorProto) -> str:

        filtered_loc = self.filter_src_location(ctx.src_locations, ctx.src_loc_code, ctx.src_loc_index)

        # Comments from current code location
        raw_comment = self.comment_for_current_location(filtered_loc)
        formatted_comment = self.format_enum_comment(ctx, raw_comment)

        # Populate the template
        return self.ENUM_VALUE_TEMPLATE \
            .replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent) \
            .replace("{QUOTED_COMMENT}", formatted_comment) \
            .replace("{ENUM_VALUE_NAME}", descriptor.name) \
            .replace("{ENUM_VALUE_NUMBER}", str(descriptor.number))

    # Python type hints

    def python_field_type(
            self, package: str,
            field: pb_desc.FieldDescriptorProto,
            message: pb_desc.DescriptorProto):

        base_type = self.python_base_type(package, field, make_relative=True, alias=True)

        # Repeated fields are either lists or maps - these need special handling
        if field.label == field.Label.LABEL_REPEATED:

            # Look to see if the base type is a nested type defined in the same message as the field
            nested_type = next(filter(
                lambda nt: base_type == f"{message.name}.{nt.name}",
                message.nested_type),
                None)

            # If a nested type is found to be a map entry type, then generate a dict
            if nested_type is not None and nested_type.options.map_entry:

                key_type = self.python_base_type(package, nested_type.field[0], make_relative=True, alias=True)
                value_type = self.python_base_type(package, nested_type.field[1], make_relative=True, alias=True)
                return f"_tp.Dict[{key_type}, {value_type}]"

            # Otherwise repeated fields are generated as lists
            else:
                return f"_tp.List[{base_type}]"

        # Fields explicitly marked optional in the proto are, of course, optional!
        elif field.proto3_optional:
            return f"_tp.Optional[{base_type}]"

        # Fields in a oneof group are also optional
        # If no oneof group is set, oneof_index will have default value of 0
        # To check if this field is really set, use HasField()
        elif field.HasField('oneof_index'):
            return f"_tp.Optional[{base_type}]"

        # Everything else should be either a (non-optional) message, an enum or a primitive
        else:
            return base_type

    def python_base_type(self, package: str, field: pb_desc.FieldDescriptorProto, make_relative=False, alias=False):

        # Messages (classes) and enums use the type name declared in the field
        if field.type == field.Type.TYPE_MESSAGE or field.type == field.Type.TYPE_ENUM:

            return self.python_type_name(package, field.type_name, make_relative, alias)

        # For built-in types, use a static mapping of proto type names
        if field.type in self.PRIMITIVE_TYPE_MAPPING:

            return self.PRIMITIVE_TYPE_MAPPING[field.type].__name__

        # Any unrecognised type is an error
        raise ECodeGeneration(
            "Unknown type in protobuf field descriptor: field = {}, type code = {}"
            .format(field.name, field.type))

    @staticmethod
    def python_type_name(package: str, proto_type_name: str, make_relative=False, alias=False):

        type_name = proto_type_name[1:] if proto_type_name.startswith(".") else proto_type_name

        # For types in the current package, do not qualify type names if the make_relative flag is set
        if make_relative and type_name.startswith(package):
            type_name = type_name[len(package) + 1:]

        # For TRAC generated types, imports are aliased for each sub package
        # E.g.: trac.metadata in the API becomes trac.rt.metadata (domain objects) or trac.rt.proto.metadata (proto)
        # Then import trac.rt.metadata as metadata
        if alias and type_name.startswith("trac."):
            type_name = type_name[len("trac."):]

        # We are using deferred annotations, with from __future__ import annotations
        # Type names no longer need to be quoted!

        return type_name

    # Python defaults

    def python_default_value(
            self, package: str,
            field: pb_desc.FieldDescriptorProto,
            message: pb_desc.DescriptorProto,
            types: TYPE_INFO_MAP):

        type_name = self.python_base_type(package, field)
        type_info = types.get(type_name)

        # Repeated fields are either lists or maps - these need special handling
        if field.label == field.Label.LABEL_REPEATED:

            # Look to see if the base type is a nested type defined in the same message as the field
            nested_type = next(filter(
                lambda nt: type_name.endswith(f"{message.name}.{nt.name}"),
                message.nested_type),
                None)

            # Use _dc.field to initialise dicts and lists
            if nested_type is not None and nested_type.options.map_entry:
                return "_dc.field(default_factory=dict)"
            else:
                return "_dc.field(default_factory=list)"

        # Fields in a oneof group are always optional
        elif field.HasField('oneof_index'):
            return "None"

        # Message fields are always optional and can be set to null
        elif field.type == field.Type.TYPE_MESSAGE:
            return "None"

        # Enum fields are always set to a value (the enum' zero value)
        elif field.type == field.Type.TYPE_ENUM:
            enum_type = type_info.enum
            return f"{enum_type.name}.{enum_type.value[0].name}"

        # Assume everything else is a primitive
        else:
            return "None"

    # Comments

    def comment_for_current_location(self, locations) -> tp.Optional[str]:

        # Comments from current code location
        current_loc = self.current_location(locations)

        if current_loc is not None:
            return current_loc.leading_comments
        else:
            return None

    def format_doc_comment(
            self, ctx: LocationContext,
            comment: tp.Optional[str],
            next_indent: bool = False) \
            -> tp.Optional[str]:

        translated_comment = self.translate_comment_from_proto(ctx, comment, next_indent)
        indent = ctx.indent + 1 if next_indent else ctx.indent

        if translated_comment is None or translated_comment.strip() == "":
            return ""

        if "\n" in translated_comment.strip():

            return self.COMMENT_MULTI_LINE \
                .replace("{INDENT}", self.INDENT_TEMPLATE * indent) \
                .replace("{COMMENT}", translated_comment)

        else:

            return self.COMMENT_SINGLE_LINE \
                .replace("{INDENT}", self.INDENT_TEMPLATE * indent) \
                .replace("{COMMENT}", translated_comment.strip())

    def format_enum_comment(self, ctx: LocationContext, comment: tp.Optional[str]) -> tp.Optional[str]:

        translated_comment = self.translate_comment_from_proto(ctx, comment)
        translated_comment = translated_comment.lstrip()  # Enum comments should start immediately after the quotes

        if translated_comment.strip() == "":
            return ''

        elif "\n" not in translated_comment.strip():
            return self.ENUM_COMMENT_SINGLE_LINE \
                .replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent) \
                .replace("{COMMENT}", translated_comment.strip())

        else:
            return self.ENUM_COMMENT_MULTI_LINE \
                .replace("{INDENT}", self.INDENT_TEMPLATE * ctx.indent) \
                .replace("{COMMENT}", translated_comment)

    def translate_comment_from_proto(
            self, ctx: LocationContext,
            comment: tp.Optional[str],
            use_next_indent: bool = False) \
            -> tp.Optional[str]:

        if comment is None:
            return ""

        indent = ctx.indent + 1 if use_next_indent else ctx.indent
        next_indent = self.INDENT_TEMPLATE * (indent + 1)

        translated_comment = re.sub("^(\\*\n)|/", "", comment, count=1)
        translated_comment = re.sub("\n$", "", translated_comment)
        translated_comment = re.sub("^ ?", self.INDENT_TEMPLATE * indent, translated_comment)
        translated_comment = re.sub("\\n ?", "\n" + self.INDENT_TEMPLATE * indent, translated_comment)

        # These two translations change the JavaDoc style @see annotation into RST .. seealso::
        # This format is good for Python and the API docs which are generated from Python
        # There may be some tweaking needed to make links between submodules work in the Python RT package

        # Convert @see for methods into .. seealso:: :meth:
        translated_comment = re.sub(
            r"@see ((?:\w+\.)*)(\w+\.)(\w+)\(\)",
            ".. seealso::\\n" + next_indent + ":meth:`\\2\\3 <\\1\\2\\3>`",
            translated_comment, flags=re.IGNORECASE)

        # Convert @see for classes into .. seealso:: :class:
        translated_comment = re.sub(
            r"@see ((?:\w+\.)*)(\w+)",
            ".. seealso::\\n" + next_indent + ":class:`\\2 <\\1\\2>`",
            translated_comment, flags=re.IGNORECASE)

        # Group multiple seealso statements into a single block
        translated_comment = re.sub(
            r"(:class:.*)\n\s*\.\. seealso::\n", "\\1,\n",
            translated_comment, flags=re.IGNORECASE)

        if translated_comment.strip() == "":
            return ""

        return translated_comment

    # Source location filtering

    @staticmethod
    def filter_src_location(locations, loc_type, loc_index):

        def relative_path(loc: pb_desc.SourceCodeInfo.Location):

            return pb_desc.SourceCodeInfo.Location(
                path=loc.path[2:], span=loc.span,
                leading_comments=loc.leading_comments,
                trailing_comments=loc.trailing_comments,
                leading_detached_comments=loc.leading_detached_comments)

        filtered = filter(lambda l: len(l.path) >= 2 and l.path[0] == loc_type and l.path[1] == loc_index, locations)
        return list(map(relative_path, filtered))

    @staticmethod
    def current_location(locations) -> pb_desc.SourceCodeInfo.Location:

        return next(filter(lambda l: len(l.path) == 0, locations), None)

    @staticmethod
    def index_sub_ctx(src_locations, field_number, indent):

        base_ctx = LocationContext(src_locations, field_number, 0, indent)
        return iter(map(base_ctx.for_index, it.count(0)))

    @staticmethod
    def indent_sub_ctx(ctx: LocationContext, indent: int):

        return LocationContext(ctx.src_locations, ctx.src_loc_code, ctx.src_loc_index, ctx.indent + indent)

    # Helpers

    @staticmethod
    def get_field_number(message_descriptor, field_name: str):

        field_descriptor = next(filter(
            lambda f: f.name == field_name,
            message_descriptor.DESCRIPTOR.fields), None)

        if field_descriptor is None:

            raise ECodeGeneration(
                "Field {} not found in type {}"
                .format(field_name, message_descriptor.DESCRIPTOR.name))

        return field_descriptor.number
