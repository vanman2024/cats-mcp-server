#!/usr/bin/env python3
"""
Optimize MCP tool docstrings for token efficiency

Strips verbose Args/Returns sections, keeps only essential description.
This makes the server usable in Claude Desktop/Code/Copilot.
"""
import re
from pathlib import Path


def optimize_docstring(docstring: str) -> str:
    """
    Extract just the first sentence/description from a verbose docstring.

    Before:
        Get detailed information about a specific candidate.
        Wraps: GET /candidates/{id}

        Args:
            candidate_id: The unique identifier

        Returns:
            dict: Complete candidate profile

    After:
        Get candidate profile with applications and interviews
    """
    if not docstring:
        return docstring

    # Split by newlines
    lines = docstring.strip().split('\n')

    # Find the main description (before Args/Returns/Wraps sections)
    description_lines = []
    for line in lines:
        stripped = line.strip()

        # Stop at section markers
        if any(stripped.startswith(marker) for marker in ['Args:', 'Returns:', 'Wraps:', 'Raises:', 'Example:']):
            break

        # Skip empty lines after we have content
        if stripped and description_lines:
            description_lines.append(stripped)
        elif stripped:
            description_lines.append(stripped)

    # Join and clean up
    description = ' '.join(description_lines)

    # Remove "Wraps: " lines
    description = re.sub(r'Wraps:\s*\w+\s*/[^\s]+', '', description).strip()

    # Ensure it ends with period
    if description and not description.endswith('.'):
        description += '.'

    return description


def optimize_file(filepath: Path):
    """Optimize all docstrings in a Python file"""
    content = filepath.read_text()

    # Find all docstrings after @mcp.tool()
    pattern = r'(@mcp\.tool\(\)[^\n]*\n\s*async def[^\n]+\n\s*""")(.*?)(""")'

    def replace_docstring(match):
        decorator_and_def = match.group(1)
        docstring = match.group(2)
        closing = match.group(3)

        optimized = optimize_docstring(docstring)

        return f'{decorator_and_def}{optimized}{closing}'

    optimized_content = re.sub(pattern, replace_docstring, content, flags=re.DOTALL)

    # Count changes
    original_size = len(content)
    optimized_size = len(optimized_content)
    savings = original_size - optimized_size
    savings_pct = (savings / original_size) * 100

    print(f'{filepath.name}:')
    print(f'  Original: {original_size:,} bytes')
    print(f'  Optimized: {optimized_size:,} bytes')
    print(f'  Saved: {savings:,} bytes ({savings_pct:.1f}%)')

    # Write optimized version
    filepath.write_text(optimized_content)


def main():
    """Optimize all toolset files"""
    files = [
        'toolsets_default.py',
        'toolsets_recruiting.py',
        'toolsets_data.py',
    ]

    print('🔧 Optimizing CATS MCP Server for token efficiency...\n')

    for filename in files:
        filepath = Path(filename)
        if filepath.exists():
            optimize_file(filepath)
            print()

    print('✅ Optimization complete!')
    print('\nNext steps:')
    print('1. Test locally: python server_all_tools.py')
    print('2. Commit: git add -A && git commit -m "perf: Optimize tool docstrings for token efficiency"')
    print('3. Sync: ./scripts/sync-to-standalone.sh cats-mcp-server')


if __name__ == '__main__':
    main()
