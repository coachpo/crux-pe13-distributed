// Install-only initramfs PID 1. No files opened for writing here are on UFS.
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <linux/fs.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

#define SCRATCH "/crux-ram-twrp-preinit"
#define BLOCK_CLASS SCRATCH "/sys/class/block"
#define PROFILE "/crux-ram-twrp-profile"
#define INVENTORY "/crux-ram-twrp-guard.inventory"
#define READY "/crux-ram-twrp-guard.ready"
#define PROFILE_BYTES 65

// Known LUN0 layouts, in 4096-B blocks: 0 is the former PE-only GPT, 1 the GPT that
// local/lun0-gpt.py derives from it once MIUI's cust and vendor space is reclaimed
// (its PE extents equal installer/crux_installer.c volumes[]). Accepting both lets this TWRP start, and its
// adb restore the other table, on either one; the installer itself accepts only layout 1.
#define LAYOUTS 2

struct extent {
    unsigned long long start_lba;
    unsigned long long blocks;
};

struct partition {
    const char *name;
    const char *node;
    bool writable;
    struct extent extents[LAYOUTS];
};

// The protected originals need only a label/node match. Each writable target
// additionally binds its complete extent from the live 4096-B LUN0 GPT, and all
// writable targets must match the same layout.
static const struct partition partitions[] = {
    {"userdata", "sda34", false, {{0}}},
    {"metadata", "sda19", false, {{0}}},
    {"misc", "sda11", false, {{0}}},
    {"cache", "sda29", false, {{0}}},
    {"system", "sda33", false, {{0}}},
    {"vendor", "sda32", false, {{0}}},
    {"boot", "sde49", false, {{0}}},
    {"recovery", "sda28", false, {{0}}},
    {"dtbo", "sde45", false, {{0}}},
    {"vbmeta", "sde10", false, {{0}}},
    {"pesystem", "sda35", true, {{0x123000, 0x100000}, {0x64000, 0x100000}}},
    {"pevendor", "sda36", true, {{0x223000, 0x70000}, {0x164000, 0x70000}}},
    {"peuserdata", "sda37", true, {{0x293000, 0x38c3e00}, {0x1d4000, 0x3982e00}}},
    {"pemetadata", "sda38", true, {{0x122000, 0x1000}, {0x63000, 0x1000}}},
};

// Bit L stays set while every writable target checked so far matches layout L.
static unsigned layouts = (1u << LAYOUTS) - 1;

#define PART_COUNT (sizeof partitions / sizeof partitions[0])

static int kmsg = -1;

static _Noreturn void stop(const char *where) {
    int saved = errno;
    if (kmsg >= 0) {
        dprintf(kmsg, "<3>crux-ram-twrp-preinit: %s: %s\n", where, strerror(saved));
    } else {
        dprintf(STDERR_FILENO, "crux-ram-twrp-preinit: %s: %s\n", where, strerror(saved));
    }
    // Use a plain restart, not RESTART2 with a recovery/bootloader target.
    reboot(RB_AUTOBOOT);
    for (;;) pause();
}

static void check(bool condition, const char *what) {
    if (!condition) stop(what);
}

static bool decimal(const char *s, unsigned long long *value) {
    if (*s < '0' || *s > '9') return false;
    char *end;
    errno = 0;
    *value = strtoull(s, &end, 10);
    return !errno && !*end;
}

static bool read_decimal_file(const char *path, unsigned long long *number) {
    FILE *file = fopen(path, "re");
    if (!file) return false;
    char line[64];
    bool ok = fgets(line, sizeof line, file) && strchr(line, '\n') && fgetc(file) == EOF
              && !ferror(file);
    if (ok) {
        *strchr(line, '\n') = '\0';
        ok = decimal(line, number);
    }
    return fclose(file) == 0 && ok;
}

// sysfs exports GPT names independently of /dev/block/by-name symlinks.
static bool partition_event(FILE *file, char name[128], dev_t *device) {
    char *line = NULL;
    size_t cap = 0;
    unsigned long long maj = 0, min = 0;
    bool part = false, have_min = false, valid = true;
    while (getline(&line, &cap, file) >= 0) {
        line[strcspn(line, "\n")] = '\0';
        if (!strncmp(line, "PARTNAME=", 9)) {
            size_t len = strlen(line + 9);
            if (*name || len >= 128) valid = false;
            else memcpy(name, line + 9, len + 1);
        } else if (!strncmp(line, "MAJOR=", 6)) {
            if (!decimal(line + 6, &maj) || maj > UINT_MAX) valid = false;
        } else if (!strncmp(line, "MINOR=", 6)) {
            have_min = decimal(line + 6, &min) && min <= UINT_MAX;
        } else if (!strcmp(line, "DEVTYPE=partition")) {
            part = true;
        }
    }
    free(line);
    if (!valid || ferror(file)) return false;
    if (!*name) return true;
    if (!part || !maj || !have_min) return false;
    *device = makedev((unsigned)maj, (unsigned)min);
    return major(*device) == maj && minor(*device) == min;
}

static bool discover(dev_t devices[PART_COUNT]) {
    memset(devices, 0, sizeof(dev_t) * PART_COUNT);
    DIR *dir = opendir(BLOCK_CLASS);
    check(dir != NULL, "open sysfs block class");
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(dir);
        if (!entry) {
            check(!errno, "read sysfs block class");
            break;
        }
        if (entry->d_name[0] == '.') continue;
        char path[PATH_MAX], name[128] = "";
        snprintf(path, sizeof path, BLOCK_CLASS "/%s/uevent", entry->d_name);
        FILE *file = fopen(path, "re");
        if (!file && errno == ENOENT) continue;
        check(file != NULL, "open block uevent");
        dev_t device = 0;
        check(partition_event(file, name, &device), "parse block uevent");
        check(fclose(file) == 0, "close block uevent");
        if (!*name) continue;
        for (size_t i = 0; i < PART_COUNT; i++) {
            if (strcmp(name, partitions[i].name)) continue;
            check(!devices[i] && !strcmp(entry->d_name, partitions[i].node), name);
            snprintf(path, sizeof path, BLOCK_CLASS "/%s", entry->d_name);
            char resolved[PATH_MAX];
            const char *ufs = SCRATCH "/sys/devices/platform/soc/1d84000.ufshc/";
            check(realpath(path, resolved) != NULL && !strncmp(resolved, ufs, strlen(ufs)), name);
            devices[i] = device;
            break;
        }
    }
    check(closedir(dir) == 0, "close sysfs block class");
    for (size_t i = 0; i < PART_COUNT; i++) {
        if (!devices[i]) return false;
        for (size_t j = 0; j < i; j++) check(devices[i] != devices[j], "duplicate block identity");
    }
    return true;
}

static void protect(const struct partition *part, dev_t device, int inventory) {
    char path[PATH_MAX];
    snprintf(path, sizeof path, SCRATCH "/%s", part->name);
    check(mknod(path, S_IFBLK | 0600, device) == 0, part->name);
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    check(fd >= 0 && fstat(fd, &info) == 0 && S_ISBLK(info.st_mode) && info.st_rdev == device,
          part->name);
    int want = !part->writable, actual = -1;
    if (want) check(ioctl(fd, BLKROSET, &want) == 0, part->name);
    check(ioctl(fd, BLKROGET, &actual) == 0 && actual == want, part->name);
    if (!want) {
        unsigned long long bytes, start;
        int sector_size;
        snprintf(path, sizeof path, BLOCK_CLASS "/%s/start", part->node);
        check(ioctl(fd, BLKSSZGET, &sector_size) == 0 && sector_size == 4096 &&
              ioctl(fd, BLKGETSIZE64, &bytes) == 0 && read_decimal_file(path, &start), part->name);
        unsigned match = 0;
        for (unsigned l = 0; l < LAYOUTS; l++) {
            if (bytes == part->extents[l].blocks * 4096 && start == part->extents[l].start_lba * 8)
                match |= 1u << l;
        }
        layouts &= match;
        check(layouts != 0, part->name);
    }
    check(close(fd) == 0, part->name);
    check(dprintf(inventory, "%s %s %u:%u ro=%d\n",
                  part->name, part->node, major(device), minor(device), actual) > 0,
          "write RAM inventory");
}

static void read_profile(char profile[PROFILE_BYTES]) {
    int fd = open(PROFILE, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    check(fd >= 0 && fstat(fd, &info) == 0 && S_ISREG(info.st_mode) &&
          info.st_uid == 0 && info.st_size == PROFILE_BYTES, "open RAM profile");
    check(read(fd, profile, PROFILE_BYTES) == PROFILE_BYTES && close(fd) == 0, "read RAM profile");
    for (size_t i = 0; i < PROFILE_BYTES - 1; i++) {
        const char c = profile[i];
        check((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'), "invalid RAM profile");
    }
    check(profile[PROFILE_BYTES - 1] == '\n', "invalid RAM profile terminator");
}

int main(void) {
    // Refuse even to enumerate blocks if this is accidentally run as a tool.
    if (getpid() != 1) {
        fputs("crux-ram-twrp-preinit: must be Linux initramfs PID 1\n", stderr);
        return 1;
    }
    char profile[PROFILE_BYTES];
    read_profile(profile);
    check(mkdir(SCRATCH, 0700) == 0 &&
          mount("tmpfs", SCRATCH, "tmpfs", MS_NOSUID | MS_NOEXEC, "mode=0700") == 0,
          "create private RAM scratch");
    if (mknod(SCRATCH "/kmsg", S_IFCHR | 0600, makedev(1, 11)) == 0) {
        kmsg = open(SCRATCH "/kmsg", O_WRONLY | O_NOFOLLOW | O_CLOEXEC);
    }
    check(mkdir(SCRATCH "/sys", 0700) == 0 &&
          mount("sysfs", SCRATCH "/sys", "sysfs", MS_NOSUID | MS_NODEV | MS_NOEXEC,
                NULL) == 0, "mount private sysfs");
    dev_t devices[PART_COUNT];
    bool complete = false;
    for (unsigned i = 0; i < 30 && !(complete = discover(devices)); i++) usleep(100000);
    if (!complete) {
        errno = ENODEV;
        stop("Crux GPT partitions did not all appear");
    }
    int inventory = open(INVENTORY, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0444);
    check(inventory >= 0, "create RAM inventory");
    for (size_t i = 0; i < PART_COUNT; i++) protect(&partitions[i], devices[i], inventory);
    check(close(inventory) == 0 && umount(SCRATCH "/sys") == 0, "finish RAM inventory/sysfs");
    if (kmsg >= 0) {
        dprintf(kmsg, "<6>crux-ram-twrp-preinit: originals RO, approved PE targets RW\n");
        close(kmsg);
        kmsg = -1;
    }
    check(umount(SCRATCH) == 0 && rmdir(SCRATCH) == 0, "remove private RAM scratch");
    int ready = open(READY, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0444);
    check(ready >= 0 && write(ready, profile, sizeof profile) == sizeof profile && close(ready) == 0,
          "write RAM readiness receipt");
    // Match the original /init symlink's argv[0] without forwarding bootloader
    // arguments that could switch the retained Android init into another mode.
    execl("/system/bin/init", "/init", (char *)NULL);
    stop("exec retained Android init");
}
