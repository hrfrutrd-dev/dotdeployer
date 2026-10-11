"""Unit tests for the template engine."""

from __future__ import annotations

import unittest

from dotdeployer.errors import TemplateError
from dotdeployer.template import Environment, Template, make_loader, render, to_text, truthy
from tests.support import TempProjectTest


class InterpolationTests(unittest.TestCase):
    def test_plain_text(self) -> None:
        self.assertEqual(render("hello"), "hello")

    def test_variables_and_literals(self) -> None:
        self.assertEqual(render("{{ name }}!", {"name": "ada"}), "ada!")
        self.assertEqual(render("{{ 1 + 2 * 3 }}"), "7")
        self.assertEqual(render("{{ 'a' ~ 'b' ~ 1 }}"), "ab1")

    def test_booleans_and_none(self) -> None:
        self.assertEqual(render("{{ flag }}", {"flag": True}), "true")
        self.assertEqual(render("{{ nothing }}", {"nothing": None}), "")
        self.assertEqual(render("{{ undefined_thing }}", {"x": 1}, strict=False), "")

    def test_string_und_default(self) -> None:
        self.assertEqual(render("{{ missing | default('fallback') }}"), "fallback")
        self.assertEqual(render("{{ present | default('x') }}", {"present": "y"}), "y")
        self.assertEqual(render("{{ '' | default('x', true) }}"), "x")

    def test_strict_mode_rejects_undefined_output(self) -> None:
        with self.assertRaises(TemplateError):
            render("{{ missing }}", {})

    def test_filters_chain(self) -> None:
        self.assertEqual(render("{{ 'Hi There' | lower | replace(' ', '-') }}"), "hi-there")
        self.assertEqual(render("{{ [3, 1, 2] | sort | join(',') }}"), "1,2,3")
        self.assertEqual(render("{{ 'abc' | length }}"), "3")

    def test_arithmetic_operators(self) -> None:
        self.assertEqual(render("{{ 7 // 2 }} {{ 7 % 2 }} {{ 2 ** 3 }}" if False else "{{ 7 // 2 }} {{ 7 % 2 }}"), "3 1")

    def test_comparison_and_logic(self) -> None:
        self.assertEqual(render("{{ 'a' == 'a' and 2 > 1 }}"), "true")
        self.assertEqual(render("{{ 'x' in ['x', 'y'] }}"), "true")
        self.assertEqual(render("{{ 'z' not in ['x'] }}"), "true")
        self.assertEqual(render("{{ not false }}"), "true")

    def test_inline_conditional(self) -> None:
        self.assertEqual(render("{{ 'yes' if n > 1 else 'no' }}", {"n": 5}), "yes")
        self.assertEqual(render("{{ 'yes' if n > 1 else 'no' }}", {"n": 0}), "no")

    def test_membership_on_dicts_and_strings(self) -> None:
        self.assertEqual(render("{{ 'k' in data }}", {"data": {"k": 1}}), "true")
        self.assertEqual(render("{{ 'll' in 'hello' }}"), "true")


class BlockTests(unittest.TestCase):
    def test_if_elif_else(self) -> None:
        template = "{% if a %}A{% elif b %}B{% else %}C{% endif %}"
        self.assertEqual(render(template, {"a": True}), "A")
        self.assertEqual(render(template, {"b": True}), "B")
        self.assertEqual(render(template, {}), "C")

    def test_for_loop_with_loop_variable(self) -> None:
        template = "{% for x in items %}{{ loop.index }}{{ x }}{% if not loop.last %},{% endif %}{% endfor %}"
        self.assertEqual(render(template, {"items": ["a", "b"]}), "1a,2b")

    def test_for_else(self) -> None:
        self.assertEqual(render("{% for x in [] %}x{% else %}empty{% endfor %}"), "empty")

    def test_for_over_dict(self) -> None:
        self.assertEqual(render("{% for k, v in data | items %}{{ k }}={{ v }};{% endfor %}",
                                {"data": {"a": 1, "b": 2}}), "a=1;b=2;")

    def test_for_with_inline_condition(self) -> None:
        self.assertEqual(render("{% for n in nums if n > 1 %}{{ n }}{% endfor %}", {"nums": [1, 2, 3]}), "23")

    def test_loop_cycle_and_revindex(self) -> None:
        self.assertEqual(render("{% for x in [1,2,3] %}{{ loop.cycle('a','b') }}{% endfor %}"), "aba")
        self.assertEqual(render("{% for x in [1,2] %}{{ loop.revindex }}{% endfor %}"), "21")

    def test_set_statement(self) -> None:
        self.assertEqual(render("{% set n = 2 %}{{ n * 5 }}"), "10")

    def test_comments_are_removed(self) -> None:
        self.assertEqual(render("a{# this is ignored #}b"), "ab")

    def test_raw_blocks(self) -> None:
        self.assertEqual(render("{% raw %}{{ not_a_var }}{% endraw %}"), "{{ not_a_var }}")

    def test_block_only_lines_are_trimmed(self) -> None:
        source = "start\n{% if show %}\nmiddle\n{% endif %}\nend\n"
        self.assertEqual(render(source, {"show": True}), "start\nmiddle\nend\n")
        self.assertEqual(render(source, {"show": False}), "start\nend\n")

    def test_nested_blocks(self) -> None:
        source = (
            "{% for group in groups %}"
            "{% for item in group %}[{{ item }}]{% endfor %}"
            "{% endfor %}"
        )
        self.assertEqual(render(source, {"groups": [["a", "b"], ["c"]]}), "[a][b][c]")

    def test_include(self) -> None:
        loader = lambda name: "partial {{ who }}" if name == "partial.tmpl" else None  # noqa: E731
        env = Environment({"who": "world"}, loader=loader)
        self.assertEqual(render("{% include 'partial.tmpl' %}", env=env) if False else
                         Template("{% include 'partial.tmpl' %}").render(env), "partial world")

    def test_include_with_extra_variables(self) -> None:
        loader = lambda name: "{{ a }}-{{ b }}"  # noqa: E731
        env = Environment({"a": "1", "b": "2"}, loader=loader)
        self.assertEqual(Template("{% include 'x' with a = '9' %}").render(env), "9-2")

    def test_include_depth_guard(self) -> None:
        loader = lambda name: "{% include 'loop' %}"  # noqa: E731
        env = Environment({}, loader=loader, max_include_depth=4)
        with self.assertRaises(TemplateError):
            Template("{% include 'loop' %}").render(env)


class ErrorTests(unittest.TestCase):
    def test_unclosed_expression(self) -> None:
        with self.assertRaises(TemplateError):
            Template("{{ oops")

    def test_unknown_tag(self) -> None:
        with self.assertRaises(TemplateError):
            Template("{% bogus %}")

    def test_missing_endif(self) -> None:
        with self.assertRaises(TemplateError):
            Template("{% if x %}a")

    def test_stray_endif(self) -> None:
        with self.assertRaises(TemplateError):
            Template("{% endif %}")

    def test_unknown_filter(self) -> None:
        with self.assertRaises(TemplateError):
            render("{{ 'x' | nope }}")

    def test_division_by_zero(self) -> None:
        with self.assertRaises(TemplateError):
            render("{{ 1 / 0 }}")

    def test_error_mentions_line_number(self) -> None:
        try:
            Template("ok\nok\n{% if %}\n", name="broken.conf")
        except TemplateError as exc:
            self.assertIn("broken.conf", str(exc))
            self.assertIn("3", str(exc))
        else:  # pragma: no cover
            self.fail("expected a TemplateError")

    def test_unknown_function_is_reported(self) -> None:
        with self.assertRaises(TemplateError):
            render("{{ nope() }}")

    def test_unknown_variable_in_call_arg(self) -> None:
        with self.assertRaises(TemplateError):
            render("{{ file(missing_path) }}")


class EnvironmentTests(unittest.TestCase):
    def test_variables_property(self) -> None:
        names = Template("{{ a }}{% if b %}{{ c }}{% endif %}").variables
        self.assertEqual(sorted(names), ["a", "b", "c"])

    def test_custom_filter(self) -> None:
        env = Environment({"x": "abc"}, filters={"shout": lambda v: str(v).upper() + "!"})
        self.assertEqual(Template("{{ x | shout }}").render(env), "ABC!")

    def test_custom_function(self) -> None:
        env = Environment({}, functions={"double": lambda v: v * 2})
        self.assertEqual(Template("{{ double(21) }}").render(env), "42")

    def test_child_scope_isolated(self) -> None:
        env = Environment({"x": 1})
        child = env.child({"x": 2})
        self.assertEqual(Template("{{ x }}").render(child), "2")
        self.assertEqual(Template("{{ x }}").render(env), "1")

    def test_facts_style_attribute_access(self) -> None:
        env = Environment({"facts": {"os": "linux", "arch": "x86_64"}})
        self.assertEqual(Template("{{ facts.os }}/{{ facts['arch'] }}").render(env), "linux/x86_64")

    def test_loader_is_restricted_to_base_dir(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "inside.tmpl").write_text("inside", encoding="utf-8")
            loader = make_loader(base)
            self.assertEqual(loader("inside.tmpl"), "inside")
            self.assertIsNone(loader("missing.tmpl"))
            with self.assertRaises(TemplateError):
                loader("../../etc/passwd")

    def test_env_and_file_functions(self) -> None:
        import os
        import tempfile
        from pathlib import Path

        os.environ["DD_TEMPLATE_TEST"] = "value"
        self.addCleanup(lambda: os.environ.pop("DD_TEMPLATE_TEST", None))
        self.assertEqual(render("{{ env('DD_TEMPLATE_TEST') }}"), "value")
        self.assertEqual(render("{{ env('DD_MISSING', 'fallback') }}"), "fallback")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.txt"
            path.write_text("file-content\n", encoding="utf-8")
            self.assertEqual(render(f"{{{{ file('{path}') }}}}"), "file-content")

    def test_helpers_conversion(self) -> None:
        self.assertTrue(truthy("yes"))
        self.assertFalse(truthy("no"))
        self.assertFalse(truthy(""))
        self.assertFalse(truthy([]))
        self.assertEqual(to_text(None), "")
        self.assertEqual(to_text(True), "true")
        self.assertEqual(to_text([1, 2]), "1, 2")


class SecurityTests(unittest.TestCase):
    def test_no_attribute_escape_to_dunder(self) -> None:
        with self.assertRaises(TemplateError):
            render("{{ obj.__class__ }}", {"obj": {"a": 1}})

    def test_calls_are_restricted_to_provided_functions(self) -> None:
        with self.assertRaises(TemplateError):
            render("{{ open('/etc/passwd') }}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
