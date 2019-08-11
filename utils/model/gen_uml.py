"""The Gaphor code generator.

This file provides the code generator which transforms gaphor/UML/uml2.gaphor
into gaphor/UML/uml2.py. Also a distutils tool, build_uml, is provided.

Create a UML 2.0 datamodel from the Gaphor model file.

To do this we do the following:
1. read the model file with the gaphor parser
2. Create a object hierarchy by ordering elements based on generalizations

Recreate the model using some very dynamic class, so we can set all
attributes and traverse them to generate the data model.
"""

import sys
import ast

from gaphor.storage.parser import parse, base, element
from utils.model import override

header = """# This file is generated by build_uml.py. DO NOT EDIT!

from gaphor.UML.properties import association, attribute, enumeration, derived, derivedunion, redefine
"""

# Make getitem behave more politely
base.__real_getitem__ = base.__getitem__


def base__getitem__(self, key):
    try:
        return self.__real_getitem__(key)
    except KeyError:
        return None


base.__getitem__ = base__getitem__


import re

pattern = r"([A-Z])"
sub = r"_\1"


def camelCase_to_underscore(str):
    """
    >>> camelCase_to_underscore('camelcase')
    'camelcase'
    >>> camelCase_to_underscore('camelCase')
    'camel_case'
    >>> camelCase_to_underscore('camelCamelCase')
    'camel_camel_case'
    """
    return re.sub(pattern, sub, str).lower()


_ = camelCase_to_underscore


def msg(s):
    sys.stderr.write("  ")
    sys.stderr.write(s)
    sys.stderr.write("\n")
    sys.stderr.flush()


class Writer:
    def __init__(self, filename, overrides=None):
        self.overrides = overrides
        if filename:
            self.out = hasattr(filename, "write") and filename or open(filename, "w")
        else:
            self.out = sys.stdout

    def write(self, data):
        self.out.write(data)

    def close(self):
        self.out.close()

    def write_classdef(self, clazz):
        """
        Write a class definition (class xx(x): pass).
        First the parent classes are examined. After that its own definition
        is written. It is ensured that class definitions are only written
        once.
        """
        if not clazz.written:
            s = ""
            for g in clazz.generalization:
                self.write_classdef(g)
                if s:
                    s += ", "
                s = s + g["name"]
            if not self.overrides.write_override(self, clazz["name"]):
                self.write("class {}".format(clazz["name"]))
                if s:
                    self.write(f"({s})")
                self.write(": pass\n")
        clazz.written = True

    def write_property(self, full_name, value):
        """
        Write a property to the file. If the property is overridden, use the
        overridden value. full_name should be like Class.attribute. value is
        free format text.
        """
        if not self.overrides.write_override(self, full_name):
            self.write(f"{full_name} = {value}\n")

    def write_attribute(self, a, enumerations={}):
        """
        Write a definition for attribute a. Enumerations may be a dict
        of enumerations, indexed by ID. These are used to identify enums.
        """
        params = {}
        type = a.typeValue
        if type is None:
            raise ValueError(
                "ERROR! type is not specified for property %s.%s"
                % (a.class_name, a.name)
            )

        if type.lower() == "boolean":
            type = "int"
        elif type.lower() in ("integer", "unlimitednatural"):
            type = "int"
        elif type.lower() == "string":
            type = "str"

        default = a.defaultValue
        # Make sure types are represented the Python way:
        if default and default.lower() in ("true", "false"):
            default = default.title()  # True or False...
        if default is not None:
            params["default"] = str(default)

        lower = a.lowerValue
        if lower and lower != "0":
            params["lower"] = lower
        upper = a.upperValue
        if upper == "*":
            params["upper"] = "'*'"
        elif upper and upper != "1":
            params["upper"] = upper

        # kind, derived, a.name, type, default, lower, upper = parse_attribute(a)

        full_name = f"{a.class_name}.{a.name}"
        if self.overrides.has_override(full_name):
            self.overrides.write_override(self, full_name)
        elif ast.literal_eval(a.isDerived or "0"):
            msg(
                "ignoring derived attribute %s.%s: no definition"
                % (a.class_name, a.name)
            )
        elif type.endswith("Kind") or type.endswith("Sort"):
            e = list(filter(lambda e: e["name"] == type, list(enumerations.values())))[
                0
            ]
            self.write_property(
                f"{a.class_name}.{a.name}",
                "enumeration('%s', %s, '%s')"
                % (a.name, e.enumerates, default or e.enumerates[0]),
            )
        else:
            if params:
                attribute = "attribute('{}', {}, {})".format(
                    a.name, type, ", ".join(map("=".join, list(params.items())))
                )
            else:
                attribute = f"attribute('{a.name}', {type})"
            self.write_property(f"{a.class_name}.{a.name}", attribute)

    def write_operation(self, o):
        full_name = f"{o.class_name}.{o.name}"
        if self.overrides.has_override(full_name):
            self.overrides.write_override(self, full_name)
        else:
            msg(f"No override for operation {full_name}")

    def write_association(self, head, tail):
        """
        Write an association for head.
        The association should not be a redefine or derived association.
        """
        if head.written:
            return

        assert head.navigable
        # Derived unions and redefines are handled separately
        assert not head.derived
        assert not head.redefines

        a = f"association('{head.name}', {head.opposite_class_name}"
        if head.lower not in ("0", 0):
            a += f", lower={head.lower}"
        if head.upper != "*":
            a += f", upper={head.upper}"
        if head.composite:
            a += ", composite=True"

        # Add the opposite property if the head itself is navigable:
        if tail.navigable:
            try:
                # o_derived, o_name = parse_association_name(tail['name'])
                o_name = tail.name
                o_derived = tail.derived
            except KeyError:
                msg(
                    "ERROR! no name, but navigable: %s (%s.%s)"
                    % (tail.id, tail.class_name, tail.name)
                )
            else:
                assert not (
                    head.derived and not o_derived
                ), "One end is derived, the other end not ???"
                a += f", opposite='{o_name}'"

        self.write_property(f"{head.class_name}.{head.name}", a + ")")

    def write_derivedunion(self, d):
        """
        Write a derived union. If there are no subsets a warning
        is issued. The derivedunion is still created though.

        Derived unions may be created for associations that were returned
        False by write_association().
        """
        subs = ""
        for u in d.union:
            if u.derived and not u.written:
                self.write_derivedunion(u)
            if subs:
                subs += ", "
            subs += f"{u.class_name}.{u.name}"
        if subs:
            self.write_property(
                f"{d.class_name}.{d.name}",
                "derivedunion('%s', %s, %s, %s, %s)"
                % (
                    d.name,
                    d.opposite_class_name,
                    d.lower,
                    d.upper == "*" and "'*'" or d.upper,
                    subs,
                ),
            )
        else:
            if not self.overrides.has_override(f"{d.class_name}.{d.name}"):
                msg(
                    "no subsets for derived union: %s.%s[%s..%s]"
                    % (d.class_name, d.name, d.lower, d.upper)
                )
            self.write_property(
                f"{d.class_name}.{d.name}",
                "derivedunion('%s', %s, %s, %s)"
                % (
                    d.name,
                    d.opposite_class_name,
                    d.lower,
                    d.upper == "*" and "'*'" or d.upper,
                ),
            )
        d.written = True

    def write_redefine(self, r):
        """
        Redefines may be created for associations that were returned
        False by write_association().
        """
        self.write_property(
            f"{r.class_name}.{r.name}",
            "redefine(%s, '%s', %s, %s)"
            % (r.class_name, r.name, r.opposite_class_name, r.redefines),
        )


def parse_association_name(name):
    # First remove spaces
    name = name.replace(" ", "")
    derived = False
    # Check if this is a derived union
    while name and not name[0].isalpha():
        if name[0] == "/":
            derived = True
        name = name[1:]
    return derived, name


def parse_association_tags(appliedStereotypes):
    subsets = []
    redefines = None

    for stereotype in appliedStereotypes or []:
        for slot in stereotype.slot or []:

            msg(f"scanning {slot.definingFeature.name} = {slot.value}")

            if slot.definingFeature.name == "subsets":
                value = slot.value
                # remove all whitespaces and stuff
                value = value.replace(" ", "").replace("\n", "").replace("\r", "")
                subsets = value.split(",")

            if slot.definingFeature.name == "redefines":
                value = slot.value
                # remove all whitespaces and stuff
                redefines = value.replace(" ", "").replace("\n", "").replace("\r", "")

    return subsets, redefines


def parse_association_end(head, tail):
    """
    The head association end is enriched with the following attributes:

        derived - association is a derived union or not
        name - name of the association end (name of head is found on tail)
        class_name - name of the class this association belongs to
        opposite_class_name - name of the class at the other end of the assoc.
        lower - lower multiplicity
        upper - upper multiplicity
        composite - if the association has a composite relation to the other end
        subsets - derived unions that use the association
        redefines - redefines existing associations
    """
    head.navigable = head.get("class_")
    if not head.navigable:
        # from this side, the association is not navigable
        return

    name = head.name
    if name is None:
        raise ValueError(
            "ERROR! no name, but navigable: %s (%s.%s)"
            % (head.id, head.class_name, head.name)
        )

    upper = head.upperValue or "*"
    lower = head.lowerValue or upper
    if lower == "*":
        lower = 0
    subsets, redefines = parse_association_tags(head.appliedStereotype)

    # Add the values found. These are used later to generate derived unions.
    head.class_name = head.class_["name"]
    head.opposite_class_name = head.type["name"]
    head.lower = lower
    head.upper = upper
    head.subsets = subsets
    head.composite = head.get("aggregation") == "composite"
    head.derived = int(head.isDerived or 0)
    head.redefines = redefines


def generate(filename, outfile=None, overridesfile=None):
    # parse the file
    all_elements = parse(filename)

    def resolve(val, attr):
        """Resolve references.
        """
        try:
            refs = val.references[attr]
        except KeyError:
            val.references[attr] = None
            return

        if isinstance(refs, type([])):
            unrefs = []
            for r in refs:
                unrefs.append(all_elements[r])
            val.references[attr] = unrefs
        else:
            val.references[attr] = all_elements[refs]

    overrides = override.Overrides(overridesfile)
    writer = Writer(outfile, overrides)

    # extract usable elements from all_elements. Some elements are given
    # some extra attributes.
    classes = {}
    enumerations = {}
    generalizations = {}
    associations = {}
    properties = {}
    operations = {}
    extensions = {}  # for identifying metaclasses
    for key, val in list(all_elements.items()):
        # Find classes, *Kind (enumerations) are given special treatment
        if isinstance(val, element):
            if val.type == "Class" and val.get("name"):
                if val["name"].endswith("Kind") or val["name"].endswith("Sort"):
                    enumerations[key] = val
                else:
                    # Metaclasses are removed later on (need to be checked
                    # via the Extension instances)
                    classes[key] = val
                    # Add extra properties for easy code generation:
                    val.specialization = []
                    val.generalization = []
                    val.stereotypeName = None
                    val.written = False
            elif val.type == "Generalization":
                generalizations[key] = val
            elif val.type == "Association":
                val.asAttribute = None
                associations[key] = val
            elif val.type == "Property":
                properties[key] = val
                # resolve(val, 'typeValue')
                # resolve(val, 'defaultValue')
                # resolve(val, 'lowerValue')
                # resolve(val, 'upperValue')
                resolve(val, "appliedStereotype")
                for st in val.appliedStereotype or []:
                    resolve(st, "slot")
                    for slot in st.slot or []:
                        resolve(slot, "value")
                        resolve(slot, "definingFeature")
                val.written = False
            elif val.type == "Operation":
                operations[key] = val
            elif val.type == "Extension":
                extensions[key] = val

    # find inheritance relationships
    for g in list(generalizations.values()):
        # assert g.specific and g.general
        specific = g["specific"]
        general = g["general"]
        classes[specific].generalization.append(classes[general])
        classes[general].specialization.append(classes[specific])

    # add values to enumerations:
    for e in list(enumerations.values()):
        values = []
        for key in e["ownedAttribute"]:
            values.append(str(properties[key]["name"]))
        e.enumerates = tuple(values)

    # Remove metaclasses from classes dict
    # should check for Extension.memberEnd[Property].type
    for e in list(extensions.values()):
        ends = []
        for end in e.memberEnd:
            end = all_elements[end]
            if not end["type"]:
                continue
            end.type = all_elements[end["type"]]
            ends.append(end)
        e.memberEnd = ends
        if ends:
            del classes[e.memberEnd[0].type.id]

    # create file header
    writer.write(header)

    # Tag classes with appliedStereotype
    for c in list(classes.values()):
        if c.get("appliedStereotype"):
            # Figure out stereotype name through
            # Class.appliedStereotype.classifier.name
            instSpec = all_elements[c.appliedStereotype[0]]
            sType = all_elements[instSpec.classifier[0]]
            c.stereotypeName = sType.name
            writer.write(
                "# class '%s' has been stereotyped as '%s'\n"
                % (c.name, c.stereotypeName)
            )
            # c.written = True
            def tag_children(me):
                for child in me.specialization:
                    child.stereotypeName = sType.name
                    writer.write(
                        "# class '%s' has been stereotyped as '%s' too\n"
                        % (child.name, child.stereotypeName)
                    )
                    # child.written = True
                    tag_children(child)

            tag_children(c)

    ignored_classes = set()

    # create class definitions, not for SimpleAttribute
    for c in list(classes.values()):
        if c.stereotypeName == "SimpleAttribute":
            ignored_classes.add(c)
        else:
            writer.write_classdef(c)

    # create attributes and enumerations
    derivedattributes = {}
    for c in [c for c in list(classes.values()) if c not in ignored_classes]:
        for p in c.get("ownedAttribute") or []:
            a = properties.get(p)
            # set class_name, since write_attribute depends on it
            a.class_name = c["name"]
            if not a.get("association"):
                if overrides.derives(f"{a.class_name}.{a.name}"):
                    derivedattributes[a.name] = a
                else:
                    writer.write_attribute(a, enumerations)

    # create associations, derivedunions are held back
    derivedunions = {}  # indexed by name in stead of id
    redefines = []
    for a in list(associations.values()):
        ends = []
        # Resolve some properties:
        for end in a.memberEnd:
            end = properties[end]
            end.type = classes[end["type"]]
            if end.get("class_"):
                end.class_ = classes[end["class_"]]
            else:
                end.class_ = None
            end.is_simple_attribute = False
            if end.type is not None and end.type.stereotypeName == "SimpleAttribute":
                end.is_simple_attribute = True
                a.asAttribute = end
            ends.append(end)

        for e1, e2 in ((ends[0], ends[1]), (ends[1], ends[0])):
            parse_association_end(e1, e2)

        for e1, e2 in ((ends[0], ends[1]), (ends[1], ends[0])):
            if a.asAttribute is not None:
                if a.asAttribute is e1 and e1.navigable:
                    writer.write(
                        f"# '{e2.type.name}.{e1.name}' is a simple attribute\n"
                    )
                    e1.class_name = e2.type.name
                    e1.typeValue = "str"

                    writer.write_attribute(e1, enumerations)
                    e1.written = True
                    e2.written = True
            elif e1.redefines:
                redefines.append(e1)
            elif e1.derived or overrides.derives(f"{e1.class_name}.{e1.name}"):
                assert not derivedunions.get(e1.name), (
                    "%s.%s is already in derived union set in class %s"
                    % (e1.class_name, e1.name, derivedunions.get(e1.name).class_name)
                )
                derivedunions[e1.name] = e1
                e1.union = []
                e1.written = False
            elif e1.navigable:
                writer.write_association(e1, e2)

    # create derived unions, first link the association ends to the d
    for a in (v for v in list(properties.values()) if v.subsets):
        for s in a.subsets or ():
            try:
                if a.type not in ignored_classes:
                    derivedunions[s].union.append(a)
            except KeyError:
                msg(f"not a derived union: {a.class_name}.{s}")

    # TODO: We should do something smart here, since derived attributes (mostly)
    #       may depend on other derived attributes or associations.

    for d in list(derivedattributes.values()):
        writer.write_attribute(d)

    for d in list(derivedunions.values()):
        writer.write_derivedunion(d)

    for r in redefines or ():
        msg(f"redefining {r.redefines} -> {r.class_name}.{r.name}")
        writer.write_redefine(r)

    # create operations
    for c in [c for c in list(classes.values()) if c not in ignored_classes]:
        for p in c.get("ownedOperation") or ():
            o = operations.get(p)
            o.class_name = c["name"]
            writer.write_operation(o)

    writer.close()


if __name__ == "__main__":
    import doctest

    doctest.testmod()
    generate("uml2.gaphor")
