function! FindBibKeys()
    let l:tmpfile = tempname()
    execute "!bibrarian -k " . tmpfile
    if filereadable(tmpfile)
        for l:keys in readfile(tmpfile, '', 1)
            call feedkeys(keys)
        endfor
    endif
endfunction

autocmd FileType      tex imap <C-T> <C-O>:call FindBibKeys()<CR>
autocmd FileType plaintex imap <C-T> <C-O>:call FindBibKeys()<CR>

