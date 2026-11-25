// Install-only initramfs PID 1. No files opened for writing here are on UFS.
//
// Two outcomes, decided from sysfs before any ioctl:
//  - guarded: every named partition below sits on its expected node and the four PE
//    targets match one known LUN0 layout. The ten originals become read-only, the PE
//    targets stay writable, and /crux-ram-twrp-guard.ready lets the installer run.
//  - read-only: any other table (the official MIUI GPT before a first install, a
//    moved node, an unknown extent). Every UFS block node, whole LUNs and all their
//    partitions, becomes read-only, /crux-ram-twrp-guard.readonly records it and no
//    ready receipt is written, so the installer refuses; adb can still read the GPT
//    and back up partitions.
// Either way TWRP then starts. Its recovery service keeps no CAP_SYS_ADMIN, so it
// cannot clear BLKROSET.
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

#ifndef SCRATCH
#define SCRATCH "/crux-ram-twrp-preinit"
#endif
#define BLOCK_CLASS SCRATCH "/sys/class/block"
#define UFS_HOST SCRATCH "/sys/devices/platform/soc/1d84000.ufshc/"
#define PROFILE "/crux-ram-twrp-profile"
#define INVENTORY "/crux-ram-twrp-guard.inventory"
#define READY "/crux-ram-twrp-guard.ready"
#define READONLY "/crux-ram-twrp-guard.readonly"
#define PROFILE_BYTES 65
#define MAX_NODES 512

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

#define PART_COUNT (sizeof partitions / sizeof partitions[0])

// The six UFS LUNs; read-only mode waits for all of them before protecting anything.
static const char *const luns[] = {"sda", "sdb", "sdc", "sdd", "sde", "sdf"};
#define LUN_COUNT (sizeof luns / sizeof luns[0])

// One sysfs block entry. start and size are in 512-B sectors, as sysfs reports them.
struct node {
    char name[32];
    char partname[128];
    dev_t device;
    unsigned long long start, size;
    bool partition, ufs;
};

#ifndef CRUX_GUARD_TEST
static int kmsg = -1;
#endif

static _Noreturn void stop(const char *where) {
    int saved = errno;
#ifdef CRUX_GUARD_TEST
    fprintf(stderr, "stop: %s: %s\n", where, strerror(saved));
    exit(2);
#else
    if (kmsg >= 0) {
        dprintf(kmsg, "<3>crux-ram-twrp-preinit: %s: %s\n", where, strerror(saved));
    } else {
        dprintf(STDERR_FILENO, "crux-ram-twrp-preinit: %s: %s\n", where, strerror(saved));
    }
    // Use a plain restart, not RESTART2 with a recovery/bootloader target.
    reboot(RB_AUTOBOOT);
    for (;;) pause();
#endif
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
static bool parse_uevent(FILE *file, struct node *node) {
    char *line = NULL;
    size_t cap = 0;
    unsigned long long maj = 0, min = 0;
    bool have_maj = false, have_min = false, disk = false, valid = true;
    while (getline(&line, &cap, file) >= 0) {
        line[strcspn(line, "\n")] = '\0';
        if (!strncmp(line, "PARTNAME=", 9)) {
            size_t len = strlen(line + 9);
            if (*node->partname || len >= sizeof node->partname) valid = false;
            else memcpy(node->partname, line + 9, len + 1);
        } else if (!strncmp(line, "MAJOR=", 6)) {
            have_maj = decimal(line + 6, &maj) && maj && maj <= UINT_MAX;
        } else if (!strncmp(line, "MINOR=", 6)) {
            have_min = decimal(line + 6, &min) && min <= UINT_MAX;
        } else if (!strcmp(line, "DEVTYPE=partition")) {
            node->partition = true;
        } else if (!strcmp(line, "DEVTYPE=disk")) {
            disk = true;
        }
    }
    free(line);
    if (!valid || ferror(file) || !have_maj || !have_min || node->partition == disk) return false;
    if (*node->partname && !node->partition) return false;
    node->device = makedev((unsigned)maj, (unsigned)min);
    return major(node->device) == maj && minor(node->device) == min;
}

// Every block entry of sysfs, with its UFS ownership and extent. Fatal only on
// malformed sysfs, never on an unexpected table.
static size_t scan(struct node nodes[MAX_NODES]) {
    size_t count = 0;
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
        char path[PATH_MAX];
        snprintf(path, sizeof path, BLOCK_CLASS "/%s/uevent", entry->d_name);
        FILE *file = fopen(path, "re");
        if (!file && errno == ENOENT) continue;
        check(file != NULL, "open block uevent");
        check(count < MAX_NODES && strlen(entry->d_name) < sizeof nodes[0].name, "too many block nodes");
        struct node *node = &nodes[count];
        memset(node, 0, sizeof *node);
        strcpy(node->name, entry->d_name);
        check(parse_uevent(file, node), "parse block uevent");
        check(fclose(file) == 0, "close block uevent");
        snprintf(path, sizeof path, BLOCK_CLASS "/%s", entry->d_name);
        char resolved[PATH_MAX];
        check(realpath(path, resolved) != NULL, "resolve block node");
        node->ufs = !strncmp(resolved, UFS_HOST, strlen(UFS_HOST));
        snprintf(path, sizeof path, BLOCK_CLASS "/%s/size", entry->d_name);
        check(read_decimal_file(path, &node->size), "read block size");
        if (node->partition) {
            snprintf(path, sizeof path, BLOCK_CLASS "/%s/start", entry->d_name);
            check(read_decimal_file(path, &node->start), "read partition start");
        }
        count++;
    }
    check(closedir(dir) == 0, "close sysfs block class");
    for (size_t i = 0; i < count; i++)
        for (size_t j = 0; j < i; j++)
            check(nodes[i].device != nodes[j].device, "duplicate block identity");
    return count;
}

// The layout bit mask every writable target matches, or 0 when the table is not a
// known Crux layout. index[] receives the node of each named partition.
static unsigned classify(const struct node *nodes, size_t count, size_t index[PART_COUNT]) {
    unsigned layouts = (1u << LAYOUTS) - 1;
    for (size_t i = 0; i < PART_COUNT; i++) {
        const struct partition *part = &partitions[i];
        size_t found = 0;
        for (size_t n = 0; n < count; n++) {
            if (strcmp(nodes[n].partname, part->name)) continue;
            if (found++) return 0;
            index[i] = n;
        }
        if (found != 1) return 0;
        const struct node *node = &nodes[index[i]];
        if (!node->ufs || strcmp(node->name, part->node)) return 0;
        if (!part->writable) continue;
        unsigned match = 0;
        for (unsigned l = 0; l < LAYOUTS; l++) {
            if (node->size == part->extents[l].blocks * 8 && node->start == part->extents[l].start_lba * 8)
                match |= 1u << l;
        }
        layouts &= match;
    }
    return layouts;
}

static bool all_luns(const struct node *nodes, size_t count) {
    for (size_t l = 0; l < LUN_COUNT; l++) {
        bool seen = false;
        for (size_t n = 0; n < count && !seen; n++)
            seen = nodes[n].ufs && !nodes[n].partition && !strcmp(nodes[n].name, luns[l]);
        if (!seen) return false;
    }
    return true;
}

#ifdef CRUX_GUARD_TEST
// Host fixture entry: report the decision and the read-only set for a fake sysfs.
int main(void) {
    static struct node nodes[MAX_NODES];
    size_t count = scan(nodes), index[PART_COUNT];
    unsigned layouts = classify(nodes, count, index);
    if (layouts) {
        printf("guarded %u\n", layouts);
        for (size_t i = 0; i < PART_COUNT; i++)
            printf("%s %s %s\n", partitions[i].name, nodes[index[i]].name, partitions[i].writable ? "rw" : "ro");
        return 0;
    }
    printf("read-only %s\n", all_luns(nodes, count) ? "luns-complete" : "luns-missing");
    for (size_t n = 0; n < count; n++)
        if (nodes[n].ufs) printf("%s ro\n", nodes[n].name);
    return 0;
}
#else

static int open_node(const char *label, dev_t device) {
    char path[PATH_MAX];
    snprintf(path, sizeof path, SCRATCH "/%s", label);
    check(mknod(path, S_IFBLK | 0600, device) == 0, label);
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    check(fd >= 0 && fstat(fd, &info) == 0 && S_ISBLK(info.st_mode) && info.st_rdev == device, label);
    return fd;
}

static void set_read_only(int fd, const char *label) {
    int want = 1, actual = -1;
    check(ioctl(fd, BLKROSET, &want) == 0 && ioctl(fd, BLKROGET, &actual) == 0 && actual == 1, label);
}

static void protect(const struct partition *part, const struct node *node, int inventory) {
    int fd = open_node(part->name, node->device);
    int actual = -1;
    if (!part->writable) set_read_only(fd, part->name);
    check(ioctl(fd, BLKROGET, &actual) == 0 && actual == !part->writable, part->name);
    if (part->writable) {
        // The ioctl view must agree with the sysfs extent the layout decision used.
        unsigned long long bytes;
        int sector_size;
        check(ioctl(fd, BLKSSZGET, &sector_size) == 0 && sector_size == 4096 &&
              ioctl(fd, BLKGETSIZE64, &bytes) == 0 && bytes == node->size * 512, part->name);
    }
    check(close(fd) == 0, part->name);
    check(dprintf(inventory, "%s %s %u:%u ro=%d\n",
                  part->name, node->name, major(node->device), minor(node->device), actual) > 0,
          "write RAM inventory");
}

// Protect every UFS block node, then rescan until a pass finds no new node.
static void protect_all(int inventory) {
    static struct node nodes[MAX_NODES];
    static dev_t done[MAX_NODES];
    size_t done_count = 0;
    for (unsigned quiet = 0, pass = 0; quiet < 3; pass++) {
        check(pass < 100, "UFS block nodes keep appearing");
        size_t count = scan(nodes);
        bool added = false;
        for (size_t n = 0; n < count; n++) {
            if (!nodes[n].ufs) continue;
            bool seen = false;
            for (size_t d = 0; d < done_count && !seen; d++) seen = done[d] == nodes[n].device;
            if (seen) continue;
            int fd = open_node(nodes[n].name, nodes[n].device);
            set_read_only(fd, nodes[n].name);
            check(close(fd) == 0, nodes[n].name);
            check(done_count < MAX_NODES, "too many UFS block nodes");
            done[done_count++] = nodes[n].device;
            check(dprintf(inventory, "%s %s %u:%u ro=1\n", nodes[n].partname[0] ? nodes[n].partname : "-",
                          nodes[n].name, major(nodes[n].device), minor(nodes[n].device)) > 0,
                  "write RAM inventory");
            added = true;
        }
        quiet = added ? 0 : quiet + 1;
        if (!added) usleep(100000);
    }
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

static void receipt(const char *path, const char profile[PROFILE_BYTES]) {
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0444);
    check(fd >= 0 && write(fd, profile, PROFILE_BYTES) == PROFILE_BYTES && close(fd) == 0,
          "write RAM guard receipt");
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
    static struct node nodes[MAX_NODES];
    size_t count = 0, index[PART_COUNT];
    unsigned layouts = 0;
    for (unsigned i = 0; i < 30; i++) {
        count = scan(nodes);
        if ((layouts = classify(nodes, count, index))) break;
        usleep(100000);
    }
    int inventory = open(INVENTORY, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0444);
    check(inventory >= 0, "create RAM inventory");
    if (layouts) {
        for (size_t i = 0; i < PART_COUNT; i++) protect(&partitions[i], &nodes[index[i]], inventory);
    } else {
        // Not a known layout: wait for every LUN, then make all of UFS read-only.
        for (unsigned i = 0; i < 50 && !all_luns(nodes, count); i++) {
            usleep(100000);
            count = scan(nodes);
        }
        if (!all_luns(nodes, count)) {
            errno = ENODEV;
            stop("UFS LUNs did not all appear");
        }
        check(dprintf(inventory, "read-only: unknown LUN0 layout\n") > 0, "write RAM inventory");
        protect_all(inventory);
    }
    check(close(inventory) == 0 && umount(SCRATCH "/sys") == 0, "finish RAM inventory/sysfs");
    if (kmsg >= 0) {
        dprintf(kmsg, layouts ? "<6>crux-ram-twrp-preinit: originals RO, approved PE targets RW\n"
                              : "<4>crux-ram-twrp-preinit: unknown LUN0 layout, all UFS RO, install disabled\n");
        close(kmsg);
        kmsg = -1;
    }
    check(umount(SCRATCH) == 0 && rmdir(SCRATCH) == 0, "remove private RAM scratch");
    receipt(layouts ? READY : READONLY, profile);
    // Match the original /init symlink's argv[0] without forwarding bootloader
    // arguments that could switch the retained Android init into another mode.
    execl("/system/bin/init", "/init", (char *)NULL);
    stop("exec retained Android init");
}
#endif
