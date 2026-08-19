import { Crepe } from '@milkdown/crepe';
import '@milkdown/crepe/theme/common/style.css';
import '@milkdown/crepe/theme/frame-dark.css';

import { commandsCtx, editorViewCtx, schemaCtx } from '@milkdown/kit/core';
import { $prose, callCommand } from '@milkdown/kit/utils';
import { Plugin, PluginKey } from '@milkdown/kit/prose/state';
import { Decoration, DecorationSet } from '@milkdown/kit/prose/view';
import {
  createCodeBlockCommand,
  insertHrCommand,
  insertImageCommand,
  liftListItemCommand,
  sinkListItemCommand,
  toggleEmphasisCommand,
  toggleInlineCodeCommand,
  toggleLinkCommand,
  toggleStrongCommand,
  turnIntoTextCommand,
  wrapInBlockquoteCommand,
  wrapInBulletListCommand,
  wrapInHeadingCommand,
  wrapInOrderedListCommand,
} from '@milkdown/kit/preset/commonmark';
import {
  insertTableCommand,
  toggleStrikethroughCommand,
} from '@milkdown/kit/preset/gfm';
import { redoCommand, undoCommand } from '@milkdown/kit/plugin/history';

window.CarrotCrepe = Crepe;

// The format bar needs to *run* editor commands and to *read* which ones are
// active, and Crepe exports neither — it exports the editor. Everything the
// bar touches is listed here by name rather than the whole kit: a command the
// bar cannot press is a command that does not need to cross the bundle
// boundary, and the list is the contract between the two halves.
//
// `ctx` slices come along for the ride because "is the cursor bold" is not a
// command — it is a question about the current selection, answered from the
// view and the schema.
// Groups draw their chip as a ProseMirror decoration rather than as classes on
// the editor's nodes. That is not a style preference: a class put on
// ProseMirror's own DOM survives about 120ms, because the editor rebuilds that
// DOM from its document state and drops anything it did not put there. A
// decoration is part of the state, so the editor redraws the chip instead of
// discarding it — which means these four have to cross the bundle boundary.
// `$prose` is how a bare ProseMirror plugin becomes something `editor.use()`
// accepts.
window.CarrotMilkdownKit = {
  ctx: { commandsCtx, editorViewCtx, schemaCtx },
  callCommand,
  $prose,
  prose: { Plugin, PluginKey, Decoration, DecorationSet },
  commands: {
    undo: undoCommand,
    redo: redoCommand,
    strong: toggleStrongCommand,
    emphasis: toggleEmphasisCommand,
    strikethrough: toggleStrikethroughCommand,
    inlineCode: toggleInlineCodeCommand,
    link: toggleLinkCommand,
    heading: wrapInHeadingCommand,
    paragraph: turnIntoTextCommand,
    blockquote: wrapInBlockquoteCommand,
    codeBlock: createCodeBlockCommand,
    bulletList: wrapInBulletListCommand,
    orderedList: wrapInOrderedListCommand,
    sinkListItem: sinkListItemCommand,
    liftListItem: liftListItemCommand,
    hr: insertHrCommand,
    image: insertImageCommand,
    table: insertTableCommand,
  },
};
