/* Native host test for the same quoting code compiled into the Windows PE.
 * It verifies argument encoding only; it does not emulate Windows execution.
 * Build: zig cc -O2 -Wall -Wextra -Werror packaging/test_launcher_args.c -o launcher-args-test
 */
#define FIGURE_WORKBENCH_LAUNCHER_TEST
#include "windows_launcher.c"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>

static void check_argument(const wchar_t *input, const wchar_t *expected) {
    wchar_t storage[1024] = {0};
    CommandBuffer command = {storage, 0, 1024};
    assert(append_argument(&command, input));
    assert(wcscmp(command.text, expected) == 0);
}

int main(void) {
    check_argument(L"", L"\"\"");
    check_argument(L"plain", L"\"plain\"");
    check_argument(L"two words", L"\"two words\"");
    check_argument(L"tab\there", L"\"tab\there\"");
    check_argument(L"a\"b", L"\"a\\\"b\"");
    check_argument(L"a\\\"b", L"\"a\\\\\\\"b\"");
    check_argument(L"\\", L"\"\\\\\"");
    check_argument(L"C:\\研究 项目\\", L"\"C:\\研究 项目\\\\\"");
    check_argument(L"C:\\nested\\file.txt", L"\"C:\\nested\\file.txt\"");
    check_argument(L"& | ^ % $ ;", L"\"& | ^ % $ ;\"");
    wchar_t storage[128] = {0};
    CommandBuffer command = {storage, 0, 128};
    assert(append_argument(&command, L"C:\\App Folder\\python.exe"));
    assert(append_argument(&command, L""));
    assert(append_argument(&command, L"last\\"));
    assert(wcscmp(command.text, L"\"C:\\App Folder\\python.exe\" \"\" \"last\\\\\"") == 0);
    wchar_t tiny_storage[4] = {0};
    CommandBuffer tiny = {tiny_storage, 0, 4};
    assert(!append_argument(&tiny, L"too long"));
    assert(tiny.length < tiny.capacity && tiny.text[tiny.length] == L'\0');
    puts("12 launcher argument cases passed (native host quoting test). Windows process launch not executed.");
    return 0;
}
