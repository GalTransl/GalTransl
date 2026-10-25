"""Line diff algorithm adapted from jsdiff's Myers implementation.

BSD 3-Clause License

Copyright (c) 2009-2015, Kevin Decker <kpdecker@gmail.com>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""


def line_opcodes(old, new, check_stop=lambda: None):
    # Paths hold persistent linked components (count, kind, previous).
    # Preserve jsdiff's tie-breaking on repeated lines, rather than using LCS heuristics.
    def common(position, last, diagonal):
        other = position - diagonal
        count = 0
        while other + 1 < len(new) and position + 1 < len(old) and old[position + 1] == new[other + 1]:
            if count % 4096 == 0:
                check_stop()
            position += 1
            other += 1
            count += 1
        if count:
            last = (count, 'equal', last)
        return position, last, other

    def finish(last):
        components = []
        while last:
            count, kind, previous = last
            components.append((count, kind))
            last = previous
        a = c = 0
        result = []
        for count, kind in reversed(components):
            b = a + (count if kind != 'insert' else 0)
            d = c + (count if kind != 'delete' else 0)
            result.append((kind, a, b, c, d))
            a, c = b, d
        return result

    position, last, other = common(-1, None, 0)
    if position + 1 >= len(old) and other + 1 >= len(new):
        return finish(last)
    paths = {0: (position, last)}
    low, high = -float('inf'), float('inf')
    for distance in range(1, len(old) + len(new) + 1):
        check_stop()
        for diagonal in range(int(max(low, -distance)), int(min(high, distance)) + 1, 2):
            remove = paths.pop(diagonal - 1, None)
            add = paths.get(diagonal + 1)
            can_add = add is not None and 0 <= add[0] - diagonal < len(new)
            can_remove = remove is not None and remove[0] + 1 < len(old)
            if not can_add and not can_remove:
                paths.pop(diagonal, None)
                continue
            if not can_remove or can_add and remove[0] < add[0]:
                position, last = add
                kind = 'insert'
            else:
                position, last = remove
                position += 1
                kind = 'delete'
            if last and last[1] == kind:
                last = (last[0] + 1, kind, last[2])
            else:
                last = (1, kind, last)
            position, last, other = common(position, last, diagonal)
            if position + 1 >= len(old) and other + 1 >= len(new):
                return finish(last)
            paths[diagonal] = position, last
            if position + 1 >= len(old):
                high = min(high, diagonal - 1)
            if other + 1 >= len(new):
                low = max(low, diagonal + 1)
    raise AssertionError('No diff path found')


def unified_patch(path, before, after, opcodes):
    result = [f'--- {path}\n', f'+++ {path}\n']
    parts = [(tag, before[a:b] if tag != 'insert' else after[c:d]) for tag, a, b, c, d in opcodes]
    parts.append(('equal', []))
    old_start = new_start = 0
    old_line = new_line = 1
    current = []
    for index, (kind, lines) in enumerate(parts):
        if kind != 'equal':
            if not old_start:
                old_start, new_start = old_line, new_line
                if index:
                    context = parts[index - 1][1][-4:]
                    current = [' ' + line for line in context]
                    old_start -= len(context)
                    new_start -= len(context)
            current.extend(('+' if kind == 'insert' else '-') + line for line in lines)
            if kind == 'insert':
                new_line += len(lines)
            else:
                old_line += len(lines)
        else:
            if old_start:
                if len(lines) <= 8 and index < len(parts) - 2:
                    current.extend(' ' + line for line in lines)
                else:
                    context = min(len(lines), 4)
                    current.extend(' ' + line for line in lines[:context])
                    old_count = old_line - old_start + context
                    new_count = new_line - new_start + context
                    result.append(f'@@ -{old_start if old_count else old_start - 1},{old_count} +{new_start if new_count else new_start - 1},{new_count} @@\n')
                    for line in current:
                        result.append(line)
                        if not line.endswith('\n'):
                            result.append('\n\\ No newline at end of file\n')
                    old_start = new_start = 0
                    current = []
            old_line += len(lines)
            new_line += len(lines)
    return ''.join(result)
