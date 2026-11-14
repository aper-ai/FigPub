/* Figure Workbench portable Windows launcher. AGPL-3.0-only.
 * Layout: FigureWorkbench.exe, runtime\pythonw.exe, app\run.py.
 * No shell, PATH lookup, installed Python, pip, or administrator access is used.
 */
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <wchar.h>

#define COMMAND_CAPACITY 32767u

typedef struct {
    wchar_t *text;
    size_t length;
    size_t capacity;
} CommandBuffer;

static int append_character(CommandBuffer *buffer, wchar_t character) {
    if (buffer->length + 1 >= buffer->capacity) return 0;
    buffer->text[buffer->length++] = character;
    buffer->text[buffer->length] = L'\0';
    return 1;
}

static int append_repeated(CommandBuffer *buffer, wchar_t character, size_t count) {
    while (count--) if (!append_character(buffer, character)) return 0;
    return 1;
}

/* Microsoft CRT argument rules: backslashes before quotes and the closing
 * quote must be doubled. Always quoting also preserves empty arguments.
 */
static int append_argument(CommandBuffer *buffer, const wchar_t *argument) {
    size_t backslashes = 0;
    if (buffer->length && !append_character(buffer, L' ')) return 0;
    if (!append_character(buffer, L'"')) return 0;
    for (const wchar_t *cursor = argument; *cursor; ++cursor) {
        if (*cursor == L'\\') { ++backslashes; continue; }
        if (*cursor == L'"') {
            if (!append_repeated(buffer, L'\\', backslashes * 2 + 1)) return 0;
        } else if (!append_repeated(buffer, L'\\', backslashes)) return 0;
        backslashes = 0;
        if (!append_character(buffer, *cursor)) return 0;
    }
    return append_repeated(buffer, L'\\', backslashes * 2) && append_character(buffer, L'"');
}

#ifndef FIGURE_WORKBENCH_LAUNCHER_TEST
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>

/* GUI subsystem: startup failures stay visible without a console window. */
static int report_failure(const wchar_t *message, DWORD error, const wchar_t *path) {
    const wchar_t *intro = L"图研无法启动。\n\n";
    wchar_t *system = NULL;
    if (error) FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
                             FORMAT_MESSAGE_IGNORE_INSERTS, NULL, error, 0, (LPWSTR)&system, 0, NULL);
    size_t length = wcslen(intro) + wcslen(message) + (path ? wcslen(path) : 0) +
                    (system ? wcslen(system) : 0) + 128;
    wchar_t *detail = (wchar_t *)calloc(length, sizeof(wchar_t));
    if (detail) {
        swprintf(detail, length, L"%ls%ls\n\n%ls%ls\n%ls", intro, message,
                 path ? L"需要的文件：" : L"", path ? path : L"", system ? system : L"");
        MessageBoxW(NULL, detail, L"图研 · Figure Workbench", MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
        free(detail);
    } else MessageBoxW(NULL, message, L"Figure Workbench", MB_OK | MB_ICONERROR);
    if (system) LocalFree(system);
    return (int)(error ? error : 1);
}

static wchar_t *launcher_directory(void) {
    for (DWORD capacity = 512; capacity <= 32768; capacity *= 2) {
        wchar_t *path = (wchar_t *)calloc(capacity, sizeof(wchar_t));
        if (!path) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return NULL; }
        DWORD length = GetModuleFileNameW(NULL, path, capacity);
        if (!length) { DWORD error = GetLastError(); free(path); SetLastError(error); return NULL; }
        if (length < capacity) {
            wchar_t *separator = wcsrchr(path, L'\\');
            if (!separator) { free(path); SetLastError(ERROR_BAD_PATHNAME); return NULL; }
            separator[1] = L'\0';
            return path;
        }
        free(path);
    }
    SetLastError(ERROR_FILENAME_EXCED_RANGE);
    return NULL;
}

static wchar_t *relative_file(const wchar_t *directory, const wchar_t *name) {
    size_t a = wcslen(directory), b = wcslen(name);
    if (a + b + 1 > 32768) { SetLastError(ERROR_FILENAME_EXCED_RANGE); return NULL; }
    wchar_t *path = (wchar_t *)malloc((a + b + 1) * sizeof(wchar_t));
    if (!path) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return NULL; }
    wmemcpy(path, directory, a);
    wmemcpy(path + a, name, b + 1);
    return path;
}

static int regular_file_exists(const wchar_t *path) {
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

static int run_launcher(int argc, wchar_t **argv) {
    int result = 1;
    wchar_t *directory = launcher_directory(), *python = NULL, *script = NULL;
    CommandBuffer command = {NULL, 0, COMMAND_CAPACITY};
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION child = {0};
    HANDLE family = NULL;
    if (!directory) return report_failure(L"无法确定程序目录。", GetLastError(), NULL);
    python = relative_file(directory, L"runtime\\pythonw.exe");
    script = relative_file(directory, L"app\\run.py");
    if (!python || !script) {
        result = report_failure(L"无法解析程序路径。", GetLastError(), NULL);
        goto cleanup;
    }
    if (!regular_file_exists(python) || !regular_file_exists(script)) {
        result = report_failure(L"请先完整解压客户端包，再双击 FigureWorkbench.exe。保留 runtime 与 app 文件夹。",
                                ERROR_FILE_NOT_FOUND, !regular_file_exists(python) ? python : script);
        goto cleanup;
    }
    command.text = (wchar_t *)calloc(command.capacity, sizeof(wchar_t));
    if (!command.text) { result = report_failure(L"内存不足。", ERROR_NOT_ENOUGH_MEMORY, NULL); goto cleanup; }
    if (!append_argument(&command, python) || !append_argument(&command, L"-B") || !append_argument(&command, L"-X") ||
        !append_argument(&command, L"utf8") || !append_argument(&command, L"-u") ||
        !append_argument(&command, script)) {
        result = report_failure(L"程序路径超过 Windows 限制。", ERROR_FILENAME_EXCED_RANGE, NULL);
        goto cleanup;
    }
    for (int index = 1; index < argc; ++index) {
        if (!append_argument(&command, argv[index])) {
            result = report_failure(L"启动参数超过 Windows 长度限制。", ERROR_BAD_LENGTH, NULL);
            goto cleanup;
        }
    }
    /* Own the complete process family. A host crash or Task Manager termination
     * cannot leave the local service or a model worker running indefinitely. */
    family = CreateJobObjectW(NULL, NULL);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!family || !SetInformationJobObject(family, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
        result = report_failure(L"无法准备客户端进程管理。", GetLastError(), NULL);
        goto cleanup;
    }
    startup.cb = sizeof(startup);
    if (!CreateProcessW(python, command.text, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW | CREATE_SUSPENDED, NULL, directory, &startup, &child)) {
        result = report_failure(L"无法启动随包的桌面运行环境。", GetLastError(), python);
        goto cleanup;
    }
    if (!AssignProcessToJobObject(family, child.hProcess)) {
        DWORD error = GetLastError();
        TerminateProcess(child.hProcess, 1);
        result = report_failure(L"无法管理客户端子进程。", error, NULL);
        goto cleanup;
    }
    if (ResumeThread(child.hThread) == (DWORD)-1) {
        result = report_failure(L"无法启动客户端进程。", GetLastError(), NULL);
        goto cleanup;
    }
    DWORD exit_code = 1;
    if (WaitForSingleObject(child.hProcess, INFINITE) == WAIT_OBJECT_0 && GetExitCodeProcess(child.hProcess, &exit_code)) {
        result = (int)exit_code;
    } else {
        result = report_failure(L"无法读取客户端退出状态。", GetLastError(), NULL);
    }
cleanup:
    if (child.hThread) CloseHandle(child.hThread);
    if (child.hProcess) CloseHandle(child.hProcess);
    if (family) CloseHandle(family);
    free(command.text);
    free(script);
    free(python);
    free(directory);
    return result;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR tail, int display) {
    (void)instance; (void)previous; (void)tail; (void)display;
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return report_failure(L"无法读取启动参数。", GetLastError(), NULL);
    int result = run_launcher(argc, argv);
    LocalFree(argv);
    return result;
}
#endif
