from claudegram.formatting import (
    TELEGRAM_LIMIT,
    Chunk,
    md_to_html,
    render,
    split_markdown,
    telegram_length,
)


def test_plain_text_is_escaped():
    assert md_to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_code_block_becomes_pre_code_with_language():
    html = md_to_html("Mira:\n```python\nprint(1 < 2)\n```")
    assert html == 'Mira:\n<pre><code class="language-python">print(1 &lt; 2)</code></pre>'


def test_code_block_without_language():
    assert md_to_html("```\nls -la\n```") == "<pre>ls -la</pre>"


def test_unterminated_fence_still_renders_as_code():
    assert md_to_html("```js\nconst a = 1;") == '<pre><code class="language-js">const a = 1;</code></pre>'


def test_inline_marks():
    assert md_to_html("**negrita** y *cursiva* y ~~tachado~~") == (
        "<b>negrita</b> y <i>cursiva</i> y <s>tachado</s>"
    )


def test_inline_code_is_protected_from_other_rules():
    assert md_to_html("usa `a * b ** c` aquí") == "usa <code>a * b ** c</code> aquí"


def test_snake_case_and_arithmetic_are_left_alone():
    assert md_to_html("foo_bar_baz y 2 * 3 * 4") == "foo_bar_baz y 2 * 3 * 4"


def test_headings_bullets_links_and_quotes():
    html = md_to_html("## Título\n- uno\n- dos\n> cita\n[web](https://example.com)")
    assert "<b>Título</b>" in html
    assert "• uno" in html
    assert "<blockquote>cita</blockquote>" in html
    assert '<a href="https://example.com">web</a>' in html


def test_link_with_quote_in_url_cannot_break_the_attribute():
    html = md_to_html('[x](https://e.com/?a="b")')
    assert 'href="https://e.com/?a=%22b%22"' in html


def test_render_short_text_is_one_chunk():
    chunks = render("hola")
    assert chunks == [Chunk(html="hola", plain="hola")]


def test_render_empty_text_is_no_chunks():
    assert render("   \n  ") == []


def test_long_text_is_split_under_the_limit():
    text = "\n\n".join(f"Párrafo número {i} con bastante relleno." for i in range(400))
    chunks = render(text)
    assert len(chunks) > 1
    assert all(telegram_length(c.html) <= TELEGRAM_LIMIT for c in chunks)
    # Nothing is lost in the split.
    joined = " ".join(c.plain for c in chunks)
    assert "Párrafo número 0 " in joined
    assert "Párrafo número 399 " in joined


def test_split_keeps_code_fences_balanced():
    body = "\n".join(f"linea_{i} = {i}" for i in range(400))
    chunks = split_markdown(f"```python\n{body}\n```", 500)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk
        assert chunk.startswith("```")
        assert chunk.endswith("```")
    # The language is carried into every continuation chunk.
    assert all(c.splitlines()[0] == "```python" for c in chunks)


def test_split_wraps_a_single_enormous_line():
    chunks = split_markdown("palabra " * 2000, 300)
    assert chunks
    assert all(len(c) <= 300 for c in chunks)


def test_emoji_count_as_two_units():
    assert telegram_length("🛫") == 2  # one surrogate pair, two units
    assert len("🛫") == 1
    assert telegram_length("ab") == 2


def test_html_heavy_text_is_resplit_until_it_fits():
    # 3000 '<' become 12000 characters once escaped.
    chunks = render("<" * 3000)
    assert len(chunks) > 1
    assert all(telegram_length(c.html) <= TELEGRAM_LIMIT for c in chunks)
