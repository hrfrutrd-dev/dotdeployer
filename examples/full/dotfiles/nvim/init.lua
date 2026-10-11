-- ~/.config/nvim - symlinked directory
vim.opt.number = true
vim.opt.expandtab = true
vim.opt.shiftwidth = 2
vim.opt.termguicolors = true

vim.g.mapleader = " "
vim.keymap.set("n", "<leader>w", ":w<CR>", { desc = "write" })
vim.keymap.set("n", "<leader>q", ":q<CR>", { desc = "quit" })
