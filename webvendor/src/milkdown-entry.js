import { Crepe } from '@milkdown/crepe';
import '@milkdown/crepe/theme/common/style.css';
import '@milkdown/crepe/theme/frame-dark.css';

import { commandsCtx, editorViewCtx, schemaCtx } from '@milkdown/kit/core';
import { callCommand } from '@milkdown/kit/utils';
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
window.CarrotMilkdownKit = {
  ctx: { commandsCtx, editorViewCtx, schemaCtx },
  callCommand,
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
