from tests.support.asserts import assert_error, assert_dialog_handled, assert_success
from tests.support.inline import inline
from tests.support.fixtures import create_dialog

_input = inline("<input>")
div = inline("""
<style>
div {
    position: absolute;
    margin: 0;
    border: 0;
    padding: 0;
    background-color: blue;
    left: 10px;
    top: 10px;
    width: 100px;
    height: 50px;
    }
</style>
<div>
""")

# 13.7 Get Element Rect

def test_no_browsing_context(session, create_window):
    # 13.7 step 1
    session.window_handle = create_window()
    session.close()

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_error(result, "no such window")


def test_handle_prompt_dismiss(new_session):
    # 13.7 step 2
    _, session = new_session({"alwaysMatch": {"unhandledPromptBehavior": "dismiss"}})
    session.url = inline("<input id=foo>")

    create_dialog(session)("alert", text="dismiss #1", result_var="dismiss1")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_success(result, "foo")
    assert_dialog_handled(session, "dismiss #1")

    create_dialog(session)("confirm", text="dismiss #2", result_var="dismiss2")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_success(result, "foo")
    assert_dialog_handled(session, "dismiss #2")

    create_dialog(session)("prompt", text="dismiss #3", result_var="dismiss3")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_success(result, "foo")
    assert_dialog_handled(session, "dismiss #3")


def test_handle_prompt_accept(new_session):
    # 13.7 step 2
    _, session = new_session({"alwaysMatch": {"unhandledPromptBehavior": "accept"}})
    session.url = inline("<input id=foo>")

    create_dialog(session)("alert", text="dismiss #1", result_var="dismiss1")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_success(result, "foo")
    assert_dialog_handled(session, "dismiss #1")

    create_dialog(session)("confirm", text="dismiss #2", result_var="dismiss2")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_success(result, "foo")
    assert_dialog_handled(session, "dismiss #2")

    create_dialog(session)("prompt", text="dismiss #3", result_var="dismiss3")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_success(result, "foo")
    assert_dialog_handled(session, "dismiss #3")


def test_handle_prompt_missing_value(session, create_dialog):
    # 13.7 step 2
    session.url = inline("<input id=foo>")

    create_dialog(session)("alert", text="dismiss #1", result_var="dismiss1")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_error(result, "unexpected alert open")
    assert_dialog_handled(session, "dismiss #1")

    create_dialog(session)("confirm", text="dismiss #2", result_var="dismiss2")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_error(result, "unexpected alert open")
    assert_dialog_handled(session, "dismiss #2")

    create_dialog(session)("prompt", text="dismiss #3", result_var="dismiss3")

    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_error(result, "unexpected alert open")
    assert_dialog_handled(session, "dismiss #3")


def test_element_not_found(session):
    # 13.7 Step 3
    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id="foo"))

    assert_error(result, "no such element")


def test_element_stale(session):
    # 13.7 step 4
    session.url = input
    element = session.find.css("input", all=False)
    session.refresh()
    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id=element.id))

    assert_error(result, "stale element reference")


def test_payload(session):
    # step 8
    session.url = div
    element = session.find.css("body", all=False)
    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id=element.id))

    assert result.status == 200
    value = result.body["value"]

    assert isinstance(value, dict)
    assert "width" in value
    assert "height" in value
    assert "x" in value
    assert "y" in value
    assert isinstance(value["width"], (int, float))
    assert isinstance(value["height"], (int, float))
    assert isinstance(value["x"], (int, float))
    assert isinstance(value["y"], (int, float))


def test_in_viewport(session):
    # step 8
    session.url = div
    element = session.find.css("div", all=False)
    result = session.transport.send("GET", "session/{session_id}/element/{element_id}/rect"
                                    .format(session_id=session.session_id,
                                            element_id=element.id))

    expected = session.execute_script("return arguments[0].getBoundingClientRect();",
                                      args=element)
    assert expected == {"x": 10,
                        "y": 10,
                        "width": 100,
                        "height": 50}
    assert_success(result, {"x": 10,
                            "y": 10,
                            "width": 100,
                            "height": 50})
