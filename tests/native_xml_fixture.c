#define NCUT_ALLOW_HIGHLEVEL_SEND 1
#define NCUT_HIGHLEVEL_XML_REQUEST 1
#include "../src/wechat_linux_cli/_native/native_highlevel_helper.c"

static unsigned char app_object[16], services_object[16], manager_object[0x910];
static unsigned char request_object[0x438], info_object[0x7e0], info_control[24];
static int parses, record_destroys, info_releases, sends, parse_mode;
static int fake_context(void) { return 1; }
static void fake_app(Shared *out) { *out = (Shared){app_object, app_object}; }
static void fake_services(Shared *out, void *app) {
    if (app != app_object) abort();
    *out = (Shared){services_object, services_object};
}
static void fake_manager(Shared *out, void *services) {
    if (services != services_object) abort();
    *(uintptr_t *)manager_object = 0xa8bd418;
    *(void **)(manager_object + 0x8f8) = app_object;
    *out = (Shared){manager_object, manager_object};
}
static void fake_request(Shared *out, void *unused) {
    if (unused) abort();
    memset(request_object, 0, sizeof(request_object));
    *(uintptr_t *)request_object = 0xa76fdd0;
    *(uint32_t *)(request_object + 0x7c) = 2;
    *(uintptr_t *)(request_object + 0xe8) = 0xa8981a0;
    *out = (Shared){request_object, request_object};
}
static void fake_assign(void *out, const void *data, size_t size) {
    if (!out || !data || !size) abort();
}
static void fake_record_construct(void *record) { memset(record, 0, 0x278); }
static void fake_record_parse(void *record) {
    ++parses;
    memset(info_object, 0, sizeof(info_object));
    *(uintptr_t *)info_object = parse_mode == 1 ? 1 : 0xa89de68;
    *(uint32_t *)(info_object + 0xc) = parse_mode == 2 ? 6 : 33;
    info_object[0x1e0] = parse_mode == 3 ? 0 : 8; /* SSO title length 4. */
    *(Shared *)((unsigned char *)record + 0x220) = (Shared){info_object, info_control};
}
static void release_info(Shared *info) {
    if (info->object != info_object || info->control != info_control) abort();
    ++info_releases; *info = (Shared){0};
}
static void fake_record_destroy(void *record) {
    ++record_destroys;
    Shared *info = (Shared *)((unsigned char *)record + 0x220);
    if (info->control) release_info(info);
}
static void fake_release(Shared *shared) {
    if (shared->object == request_object) {
        Shared *info = (Shared *)(request_object + 0xe8 + 0x218);
        if (info->control) release_info(info);
    }
    *shared = (Shared){0};
}
static void fake_send(void *out, void *manager, const Shared *request) {
    if (manager != manager_object || request->object != request_object ||
        *(uint32_t *)(request_object + 0x7c) != 4 ||
        *(uint32_t *)(request_object + 0xe8 + 8) != 49 ||
        *(uint32_t *)(request_object + 0xe8 + 0xc) != 33 ||
        ((Shared *)(request_object + 0xe8 + 0x218))->object != info_object) abort();
    ++sends; memset(out, 0, 0x30);
}
static void fake_destroy_result(void *result) { (void)result; }

static void run_case(const char *path, int send, int mode, int expected_failure) {
    char report_path[] = "/tmp/wechat-xml-report-XXXXXX";
    output_fd = mkstemp(report_path);
    if (output_fd < 0) abort();
    size_t path_size = strlen(path);
    payload[0] = 10; payload[1] = 0;
    payload[2] = path_size & 255; payload[3] = path_size >> 8;
    memcpy(payload + 4, "filehelper", 10); memcpy(payload + 14, path, path_size);
    payload_size = path_size + 14;
    worker_done = request_constructed = manager_verified = submission_entered = 0;
    result_returned = result_success = failure = report_failed = 0;
    parses = record_destroys = info_releases = sends = 0;
    should_send = send; parse_mode = mode;
    perform_native();
    if (!worker_done || failure != expected_failure ||
        sends != (send && !expected_failure) ||
        record_destroys != parses || info_releases != parses ||
        result_returned != sends || submission_entered != sends ||
        request_constructed != !expected_failure) abort();
    if (expected_failure == 16 && parses) abort();
    unlink(report_path);
}

int main(void) {
    context_available = fake_context;
    api = (NativeApi){fake_app, fake_services, fake_manager, fake_request, fake_assign,
                     fake_send, fake_destroy_result, fake_release, fake_record_construct,
                     fake_record_parse, fake_record_destroy};
    char path[] = "/tmp/wechat-xml-input-XXXXXX";
    int fd = mkstemp(path);
    if (fd < 0 || write(fd, "<msg/>", 6) != 6 || close(fd)) abort();
    run_case(path, 0, 0, 0);
    run_case(path, 1, 0, 0);
    run_case(path, 1, 1, 17);
    run_case(path, 1, 2, 18);
    run_case(path, 1, 3, 18);
    unlink(path);
    if (mkfifo(path, 0600)) abort();
    run_case(path, 1, 0, 16);
    unlink(path);
    puts("XML ownership and input rejection fixture passed");
    return 0;
}
