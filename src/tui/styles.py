"""Styles for the full-screen terminal application."""

TERMINAL_CSS = """
Screen { background: #101418; color: #d7dde5; }
#transcript {
    height: 1fr;
    border: none;
    padding: 0 1;
    background: #0b1016;
    color: #d7dde5;
    overflow-y: scroll;
}
.transcript-node {
    width: 1fr;
    height: auto;
    background: #0b1016;
    padding: 0;
}
.transcript-node > Contents {
    padding: 0 0 0 2;
}
.transcript-role {
    margin-bottom: 1;
    padding: 0 1;
}
.transcript-role > CollapsibleTitle {
    display: none;
}
.transcript-role > Contents {
    padding: 0 0 0 1;
}
.transcript-user {
    background: #17233a;
    color: #e6efff;
    border-left: solid #4f8cff;
}
.transcript-user > Contents {
    background: #17233a;
}
.transcript-assistant {
    background: #132b27;
    color: #e4f7f1;
    border-left: solid #35c6a3;
}
.transcript-assistant > Contents {
    background: #132b27;
}
.transcript-assistant .transcript-node {
    background: #132b27;
}
.transcript-assistant .transcript-node > Contents {
    background: #132b27;
}
.transcript-status {
    padding-left: 2;
    color: #9fc3e8;
}
.compact-progress {
    padding: 0 1 1 2;
    color: #9fc3e8;
}
.processing-progress, .transcript-tool-summary {
    padding: 0 1 1 2;
    color: #9fc3e8;
}
#separator { color: #5f6b76; height: 1; }
#status-bar {
    height: 1;
    width: 100%;
    background: #263442;
}
#status { width: 1fr; min-width: 1; padding: 0 1; background: #263442; color: #9fc3e8; }
#context-progress { width: 1fr; min-width: 1; height: 1; padding: 0 1; background: #263442; }
#completion-menu { height: auto; max-height: 8; display: none; }
#choice-panel {
    width: 1fr;
    height: auto;
    max-height: 40%;
    display: none;
    margin: 0 1;
    padding: 1 2;
    background: #17233a;
    border-left: thick #4f8cff;
    border-right: solid #314a6e;
    overflow: hidden;
}
#choice-header {
    width: 1fr;
    height: auto;
    max-height: 4;
    display: none;
    padding: 0 0 1 0;
    color: #f0f6ff;
    background: transparent;
    text-align: left;
}
#review-details {
    width: 1fr;
    height: auto;
    max-height: 8;
    display: none;
    padding: 0 0 1 0;
    background: transparent;
    text-align: left;
    overflow-y: auto;
}
#queued-messages {
    height: auto;
    max-height: 6;
    margin: 0 1;
    padding: 0 1;
    background: #1f2630;
    color: #c7d6e8;
    border-left: solid #d9a441;
    overflow-y: auto;
}
.choice-list {
    width: 1fr;
    height: auto;
    max-height: 6;
    display: none;
    background: transparent;
}
.choice-row {
    width: 1fr;
    height: auto;
    padding: 0 1;
    color: #d7dde5;
    background: #1b2a42;
    border-left: solid #314a6e;
}
.choice-row.-highlighted-choice {
    color: #ffffff;
    background: #2368a2;
    border-left: thick #f0c36a;
}
.choice-row.-selected-answer {
    color: #102033;
    background: #84d2bd;
    border-left: thick #e8f6ed;
}
.choice-label {
    width: 1fr;
    height: auto;
    text-align: left;
}
.choice-editor {
    width: 1fr;
    height: 1;
    display: none;
    border: none;
    padding: 0;
    background: #171c21;
    color: white;
}
#input-frame {
    width: 100%;
    height: auto;
    margin-bottom: 0;
    background: #171c21;
}
#input-frame.-single-line {
    outline: solid #405675;
    background: #1b2736;
}
#input-frame.-multiline {
    outline: none;
    background: #171c21;
}
#input {
    width: 100%;
    height: 1;
    min-height: 1;
    margin-bottom: 0;
    border: none;
    padding: 0;
    background: transparent;
    color: white;
}
"""
