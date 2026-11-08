/*
 * crux-installer: the crux-install/1 TWRP update-binary for Crux PE13.
 *
 *     update-binary <api-version> <pipe-fd> <zip-path>
 *
 * Runs in the RAM-only install TWRP, which the Recovery container U-Boot starts
 * with fastboot boot. The ZIP holds STORED entries: crux-install.txt
 * (manifest), crux-install.sig (P-256 ECDSA r||s over the manifest bytes),
 * zstd raw system/vendor images and both PE FITs. The key is
 * /crux/install-keys/b-p256.pub in the TWRP ramdisk.
 *
 * Before any write it checks the signature, the serial, the Recovery container
 * U-Boot marker, the TWRP guard profile and protected partitions, LUN0
 * geometry, every payload digest (as delivered and expanded) and the FIT
 * sizes, then asks for a volume-key confirmation. Writes go to pevendor and
 * pesystem with O_DIRECT readback, peuserdata and pemetadata are formatted,
 * both FITs are staged in the pesystem tail, and the "land" state block is
 * written last, so an interrupted run never lands a FIT. Exit status: 0 ready
 * to land, 1 failed. The protocol is described in
 * docs/pe13-install-protocol-2026-11-07.md of the Crux workspace.
 *
 * The host test build (-DCRUX_TEST) takes --root DIR, which redirects /dev,
 * /sys, /proc, /crux*, /system and the tools into a fixture, and
 * --confirm yes|no.
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#ifndef CRUX_TEST
#include <linux/input.h>
#endif

#include "mincrypt/p256.h"
#include "mincrypt/p256_ecdsa.h"
#include "mincrypt/sha256.h"
#include "zstd.h"

#define BLK 4096ULL
#define CHUNK (4ULL << 20)
#define STATE_REL 0xf0000ULL
#define ROM_STAGE_REL 0xf1000ULL
#define REC_STAGE_REL 0xf8000ULL
#define ROM_WINDOW_BLKS 0x3100ULL
#define REC_WINDOW_BLKS 0x3b00ULL
#define CONFIRM_SECONDS 60

#ifdef CRUX_TEST
#define DIRECT 0
#else
#define DIRECT O_DIRECT
#endif

struct volume {
	const char *name;
	uint64_t start;		/* LUN0 4096-byte block */
	uint64_t blocks;
};

static const struct volume volumes[] = {
	{ "pesystem", 0x29e4000, 0x100000 },
	{ "pevendor", 0x2ae4000, 0x70000 },
	{ "peuserdata", 0x2b54000, 0x1000000 },
	{ "pemetadata", 0x3b54000, 0x1000 },
	{ "cache", 0x30000, 0x18000 },
};

static const char *const protected_names[] = {
	"userdata", "metadata", "misc", "cache", "system", "vendor",
	"boot", "recovery", "dtbo", "vbmeta",
};

struct zentry {
	char name[96];
	uint64_t offset;
	uint64_t size;
};

struct kv {
	char key[64];
	char val[256];
};

struct range {
	uint64_t off;
	uint64_t len;
};

/* Payload bytes: the byte ranges of one ZIP entry. */
struct source {
	char name[96];
	int fd;
	uint64_t size;
	int n;
	struct range *r;
};

static const char *root = "";
static FILE *ui_pipe;
static int confirm_mode = -1;
static int zfd = -1;
static struct zentry zents[32];
static int nzents;
static struct kv manifest[96];
static int nmanifest;
static int writes_started;

static void at(char *out, size_t n, const char *fmt, ...)
{
	char tail[384];
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(tail, sizeof(tail), fmt, ap);
	va_end(ap);
	snprintf(out, n, "%s%s", root, tail);
}

static void ui(const char *fmt, ...)
{
	char msg[512];
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	if (ui_pipe) {
		fprintf(ui_pipe, "ui_print %s\nui_print\n", msg);
		fflush(ui_pipe);
	}
	fprintf(stderr, "crux-installer: %s\n", msg);
	fflush(stderr);
}

static void progress(double fraction, int seconds)
{
	if (ui_pipe) {
		fprintf(ui_pipe, "progress %.3f %d\n", fraction, seconds);
		fflush(ui_pipe);
	}
}

static void die(const char *fmt, ...)
{
	char msg[512];
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	ui("ERROR: %s", msg);
	if (writes_started)
		ui("PE volumes may be partly written and no FIT was staged for landing. "
		   "Re-run this ZIP from the install TWRP; it rewrites everything.");
	else
		ui("Nothing was written.");
	exit(1);
}

static void read_exact(int fd, void *buf, size_t n, uint64_t off)
{
	uint8_t *p = buf;

	while (n) {
		ssize_t r = pread(fd, p, n, (off_t)off);

		if (r <= 0)
			die("read failed at %" PRIu64 ": %s", off, r < 0 ? strerror(errno) : "short file");
		p += r;
		n -= (size_t)r;
		off += (uint64_t)r;
	}
}

static void write_exact(int fd, const void *buf, size_t n, uint64_t off)
{
	const uint8_t *p = buf;

	while (n) {
		ssize_t w = pwrite(fd, p, n, (off_t)off);

		if (w <= 0)
			die("write failed at %" PRIu64 ": %s", off, w < 0 ? strerror(errno) : "short write");
		p += w;
		n -= (size_t)w;
		off += (uint64_t)w;
	}
}

static void *aligned(size_t n)
{
	void *p = NULL;

	if (posix_memalign(&p, BLK, n))
		die("out of memory for %zu bytes", n);
	memset(p, 0, n);
	return p;
}

static uint32_t crc32_update(uint32_t crc, const uint8_t *p, size_t n)
{
	static uint32_t table[256];
	size_t i;

	if (!table[1])
		for (i = 0; i < 256; i++) {
			uint32_t c = (uint32_t)i;
			int k;

			for (k = 0; k < 8; k++)
				c = c & 1 ? 0xedb88320U ^ (c >> 1) : c >> 1;
			table[i] = c;
		}
	crc = ~crc;
	for (i = 0; i < n; i++)
		crc = table[(crc ^ p[i]) & 0xff] ^ (crc >> 8);
	return ~crc;
}

static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t le32(const uint8_t *p) { return (uint32_t)le16(p) | (uint32_t)le16(p + 2) << 16; }

static void read_file(const char *path, char *buf, size_t n, size_t *got)
{
	int fd = open(path, O_RDONLY);
	ssize_t r;

	if (fd < 0)
		die("cannot read %s: %s", path, strerror(errno));
	r = read(fd, buf, n - 1);
	close(fd);
	if (r < 0)
		die("cannot read %s", path);
	buf[r] = '\0';
	if (got)
		*got = (size_t)r;
}

/* ---- payload sources ---- */

static void source_read(const struct source *s, uint64_t pos, void *buf, size_t n)
{
	uint8_t *p = buf;

	if (pos + n > s->size)
		die("%s: read past its end", s->name);
	while (n) {
		uint64_t base = 0, within, avail;
		size_t take;
		int i;

		for (i = 0; i < s->n && pos >= base + s->r[i].len; i++)
			base += s->r[i].len;
		if (i == s->n)
			die("%s: block map is shorter than the asset", s->name);
		within = pos - base;
		avail = s->r[i].len - within;
		take = n < avail ? n : (size_t)avail;
		read_exact(s->fd, p, take, s->r[i].off + within);
		p += take;
		pos += take;
		n -= take;
	}
}

static void source_sha256(const struct source *s, uint8_t out[32])
{
	uint8_t *buf = malloc(1 << 20);
	uint64_t off = 0;
	SHA256_CTX c;

	if (!buf)
		die("out of memory");
	SHA256_init(&c);
	while (off < s->size) {
		size_t n = s->size - off < (1 << 20) ? (size_t)(s->size - off) : (1 << 20);

		source_read(s, off, buf, n);
		SHA256_update(&c, buf, (int)n);
		off += n;
	}
	free(buf);
	memcpy(out, SHA256_final(&c), 32);
}

/* ---- ZIP (STORED entries only) ---- */

static void zip_open(const char *path)
{
	uint8_t tail[65557], *cd, *p;
	struct stat st;
	size_t n;
	uint32_t cd_size, cd_off;
	int i, count;
	long eocd = -1;

	zfd = open(path, O_RDONLY);
	if (zfd < 0 || fstat(zfd, &st))
		die("cannot open %s: %s", path, strerror(errno));
	n = st.st_size < (off_t)sizeof(tail) ? (size_t)st.st_size : sizeof(tail);
	read_exact(zfd, tail, n, (uint64_t)st.st_size - n);
	for (i = (int)n - 22; i >= 0; i--)
		if (le32(tail + i) == 0x06054b50) {
			eocd = i;
			break;
		}
	if (eocd < 0)
		die("not a ZIP file");
	count = le16(tail + eocd + 10);
	cd_size = le32(tail + eocd + 12);
	cd_off = le32(tail + eocd + 16);
	if (count > (int)(sizeof(zents) / sizeof(zents[0])) || (uint64_t)cd_off + cd_size > (uint64_t)st.st_size)
		die("unexpected ZIP directory");
	cd = malloc(cd_size);
	if (!cd)
		die("out of memory");
	read_exact(zfd, cd, cd_size, cd_off);
	for (p = cd, i = 0; i < count; i++) {
		uint8_t local[30];
		uint16_t name_len, extra_len, comment_len;
		struct zentry *e = &zents[nzents++];

		if (p + 46 > cd + cd_size || le32(p) != 0x02014b50)
			die("bad ZIP directory entry");
		name_len = le16(p + 28);
		extra_len = le16(p + 30);
		comment_len = le16(p + 32);
		if (p + 46 + name_len > cd + cd_size || name_len >= sizeof(e->name))
			die("bad ZIP entry name");
		memcpy(e->name, p + 46, name_len);
		e->name[name_len] = '\0';
		if (le16(p + 10) != 0 || le32(p + 20) != le32(p + 24))
			die("ZIP entry %s is not STORED", e->name);
		e->size = le32(p + 24);
		read_exact(zfd, local, sizeof(local), le32(p + 42));
		if (le32(local) != 0x04034b50)
			die("bad ZIP local header for %s", e->name);
		e->offset = (uint64_t)le32(p + 42) + 30 + le16(local + 26) + le16(local + 28);
		if (e->offset + e->size > (uint64_t)st.st_size)
			die("ZIP entry %s exceeds the file", e->name);
		p += 46 + name_len + extra_len + comment_len;
	}
	free(cd);
}

static void zip_source(const char *name, struct source *s)
{
	int i;

	for (i = 0; i < nzents; i++)
		if (!strcmp(zents[i].name, name)) {
			snprintf(s->name, sizeof(s->name), "%s", name);
			s->fd = zfd;
			s->size = zents[i].size;
			s->n = 1;
			s->r = malloc(sizeof(*s->r));
			if (!s->r)
				die("out of memory");
			s->r[0].off = zents[i].offset;
			s->r[0].len = zents[i].size;
			return;
		}
	die("ZIP lacks %s", name);
}

/* ---- manifest ---- */

static void parse_manifest(const char *text, size_t n)
{
	const char *p = text, *end = text + n;

	while (p < end) {
		const char *nl = memchr(p, '\n', (size_t)(end - p)), *eq;
		size_t len = nl ? (size_t)(nl - p) : (size_t)(end - p);
		struct kv *kv;

		eq = memchr(p, '=', len);
		if (len && *p != '#') {
			if (!eq || (size_t)(eq - p) >= sizeof(kv->key) || len - (size_t)(eq - p) - 1 >= sizeof(kv->val) ||
			    nmanifest == (int)(sizeof(manifest) / sizeof(manifest[0])))
				die("malformed manifest line");
			kv = &manifest[nmanifest++];
			memcpy(kv->key, p, (size_t)(eq - p));
			kv->key[eq - p] = '\0';
			memcpy(kv->val, eq + 1, len - (size_t)(eq - p) - 1);
			kv->val[len - (size_t)(eq - p) - 1] = '\0';
		}
		p += len + 1;
	}
}

static const char *mget(const char *key)
{
	int i;

	for (i = 0; i < nmanifest; i++)
		if (!strcmp(manifest[i].key, key))
			return manifest[i].val;
	die("manifest lacks %s", key);
	return NULL;
}

static uint64_t mnum(const char *key)
{
	char *end;
	unsigned long long v = strtoull(mget(key), &end, 0);

	if (*end)
		die("manifest %s is not a number", key);
	return v;
}

static void mdigest(const char *key, uint8_t out[32])
{
	const char *v = mget(key);
	int i;

	if (strlen(v) != 64)
		die("manifest %s is not a SHA-256", key);
	for (i = 0; i < 32; i++) {
		unsigned int byte;

		if (sscanf(v + 2 * i, "%2x", &byte) != 1)
			die("manifest %s is not a SHA-256", key);
		out[i] = (uint8_t)byte;
	}
}

/* Verify the P-256 signature of the manifest text with a raw X||Y key file. */
static void verify_and_parse(const char *text, size_t len, const uint8_t sig[64], const char *key_path)
{
	uint8_t key[64], digest[32];
	char keybuf[160];
	p256_int x, y, msg, r, s;
	size_t got, i;

	/* Raw 64-byte X||Y, or the same as 128 hex digits (optionally newline-terminated). */
	read_file(key_path, keybuf, sizeof(keybuf), &got);
	if (got == sizeof(key)) {
		memcpy(key, keybuf, sizeof(key));
	} else if ((got == 128 || (got == 129 && keybuf[128] == '\n')) &&
		   strspn(keybuf, "0123456789abcdef") == 128) {
		for (i = 0; i < sizeof(key); i++) {
			unsigned int byte;

			sscanf(keybuf + 2 * i, "%2x", &byte);
			key[i] = (uint8_t)byte;
		}
	} else {
		die("install key %s is missing or malformed", key_path);
	}
	SHA256_hash(text, (int)len, digest);
	p256_from_bin(key, &x);
	p256_from_bin(key + 32, &y);
	p256_from_bin(digest, &msg);
	p256_from_bin(sig, &r);
	p256_from_bin(sig + 32, &s);
	if (!p256_ecdsa_verify(&x, &y, &msg, &r, &s))
		die("manifest signature does not verify with %s", key_path);
	parse_manifest(text, len);
}

/* ---- live device checks ---- */

static int cmdline_value(const char *cmdline, const char *key, char *out, size_t n)
{
	size_t klen = strlen(key);
	const char *p = cmdline;

	while ((p = strstr(p, key))) {
		if ((p == cmdline || p[-1] == ' ') && p[klen] == '=') {
			size_t len = strcspn(p + klen + 1, " \n");

			if (len >= n)
				return -1;
			memcpy(out, p + klen + 1, len);
			out[len] = '\0';
			return 0;
		}
		p += klen;
	}
	return -1;
}

static void node_of(const char *name, char *node, size_t n)
{
	char path[512], target[512];
	ssize_t len;
	const char *base;

	at(path, sizeof(path), "/dev/block/by-name/%s", name);
	len = readlink(path, target, sizeof(target) - 1);
	if (len <= 0)
		die("no by-name link for %s", name);
	target[len] = '\0';
	base = strrchr(target, '/');
	snprintf(node, n, "%s", base ? base + 1 : target);
}

static uint64_t sysfs_number(const char *node, const char *field)
{
	char path[512], buf[64];

	at(path, sizeof(path), "/sys/class/block/%s/%s", node, field);
	read_file(path, buf, sizeof(buf), NULL);
	return strtoull(buf, NULL, 10);
}

/*
 * Live facts of one partition from sysfs: its read-only flag (BLKROSET state)
 * and its extent in 512-byte sectors.
 */
struct part {
	char node[512];
	int ro;
	uint64_t start, size;
};

static void part_facts(const char *name, struct part *p, int extent)
{
	char path[640], buf[16];

	node_of(name, p->node, sizeof(p->node));
	at(path, sizeof(path), "/sys/class/block/%s/ro", p->node);
	read_file(path, buf, sizeof(buf), NULL);
	p->ro = buf[0] == '1' ? 1 : buf[0] == '0' ? 0 : -1;
	if (extent) {
		p->start = sysfs_number(p->node, "start");
		p->size = sysfs_number(p->node, "size");
	}
}

/* Live device checks before any write. */
static void check_device(void)
{
	char path[512], cmdline[4096], value[128], uevent[512], want[640];
	char profile[128], ready[128], mounts[16384];
	struct part part;
	size_t i;

	at(path, sizeof(path), "/proc/cmdline");
	read_file(path, cmdline, sizeof(cmdline), NULL);
	if (cmdline_value(cmdline, "androidboot.serialno", value, sizeof(value)))
		die("kernel command line has no serial number");
	if (strcmp(mget("serialno"), "any") && strcmp(mget("serialno"), value))
		die("this package is for serial %s, not %s", mget("serialno"), value);
	if (cmdline_value(cmdline, "androidboot.crux_uboot", value, sizeof(value)) ||
	    strcmp(value, mget("uboot_role")))
		die("not started by the declared Recovery container U-Boot (%s)", mget("uboot_role"));
	at(path, sizeof(path), "/crux-ram-twrp-profile");
	read_file(path, profile, sizeof(profile), NULL);
	at(path, sizeof(path), "/crux-ram-twrp-guard.ready");
	read_file(path, ready, sizeof(ready), NULL);
	profile[strcspn(profile, "\n")] = '\0';
	ready[strcspn(ready, "\n")] = '\0';
	if (!profile[0] || strcmp(profile, ready) || !strstr(mget("twrp_profiles"), profile))
		die("not running in a declared, guarded install TWRP profile");
	for (i = 0; i < sizeof(protected_names) / sizeof(protected_names[0]); i++) {
		part_facts(protected_names[i], &part, 0);
		if (part.ro != 1)
			die("protected partition %s is not read-only", protected_names[i]);
	}
	at(path, sizeof(path), "/proc/mounts");
	read_file(path, mounts, sizeof(mounts), NULL);
	for (i = 0; i < sizeof(volumes) / sizeof(volumes[0]); i++) {
		const struct volume *v = &volumes[i];

		part_facts(v->name, &part, 1);
		if (part.start != v->start * 8 || part.size != v->blocks * 8)
			die("live GPT extent of %s (%s) differs", v->name, part.node);
		/* ueventd names the by-name links after PARTNAME; read it back. */
		at(path, sizeof(path), "/sys/class/block/%s/uevent", part.node);
		read_file(path, uevent, sizeof(uevent), NULL);
		snprintf(want, sizeof(want), "PARTNAME=%s\n", v->name);
		if (!strstr(uevent, want))
			die("%s does not carry PARTNAME %s", part.node, v->name);
		snprintf(want, sizeof(want), "/dev/block/%s ", part.node);
		if (strstr(mounts, want))
			die("%s is mounted", v->name);
		snprintf(want, sizeof(want), "/by-name/%s ", v->name);
		if (strstr(mounts, want))
			die("%s is mounted", v->name);
		if (strncmp(v->name, "pe", 2) == 0 && part.ro != 0)
			die("%s is not writable", v->name);
	}
	ui("Device %s: serial, container U-Boot, recovery guard and LUN0 geometry checked", mget("serialno"));
}

/* ---- expansion of the zstd raw images ---- */

typedef void (*sink_fn)(void *ctx, const uint8_t *data, size_t n, uint64_t offset);

/* Collects expanded bytes into aligned 4 MiB chunks for the sink. */
struct acc {
	const char *what;
	uint8_t *buf;
	size_t fill;
	uint64_t produced, limit;
	sink_fn sink;
	void *ctx;
};

static void acc_flush(struct acc *a)
{
	if (!a->fill)
		return;
	if (a->produced + a->fill > a->limit)
		die("%s expands beyond its declared size", a->what);
	a->sink(a->ctx, a->buf, a->fill, a->produced);
	a->produced += a->fill;
	a->fill = 0;
}

static void acc_push(struct acc *a, const uint8_t *p, size_t n)
{
	while (n) {
		size_t take = CHUNK - a->fill < n ? CHUNK - a->fill : n;

		memcpy(a->buf + a->fill, p, take);
		a->fill += take;
		p += take;
		n -= take;
		if (a->fill == CHUNK)
			acc_flush(a);
	}
}

typedef void (*bytes_fn)(void *ctx, const uint8_t *p, size_t n);

static void raw_feed(void *ctx, const uint8_t *p, size_t n)
{
	acc_push(ctx, p, n);
}

static void stream(const struct source *s, bytes_fn fn, void *ctx)
{
	uint8_t *in = malloc(1 << 20), *out = malloc(1 << 20);
	ZSTD_DCtx *d = ZSTD_createDCtx();
	uint64_t consumed = 0;
	size_t ret = 1;

	if (!in || !out || !d)
		die("out of memory for %s", s->name);
	ZSTD_DCtx_setParameter(d, ZSTD_d_windowLogMax, 27);
	while (consumed < s->size) {
		size_t n = s->size - consumed < (1 << 20) ? (size_t)(s->size - consumed) : (1 << 20);
		ZSTD_inBuffer ib = { in, n, 0 };

		source_read(s, consumed, in, n);
		consumed += n;
		while (ib.pos < ib.size) {
			ZSTD_outBuffer ob = { out, 1 << 20, 0 };

			ret = ZSTD_decompressStream(d, &ob, &ib);
			if (ZSTD_isError(ret))
				die("%s: zstd: %s", s->name, ZSTD_getErrorName(ret));
			if (ob.pos)
				fn(ctx, out, ob.pos);
		}
	}
	if (ret != 0)
		die("%s: truncated zstd stream", s->name);
	ZSTD_freeDCtx(d);
	free(in);
	free(out);
}

struct image {
	const char *part;
	struct source src;
	uint64_t bytes;		/* expanded raw size */
	uint8_t sha[32];	/* expanded raw SHA-256 */
};

static uint64_t expand(const struct image *img, sink_fn sink, void *ctx)
{
	struct acc a = { img->part, aligned(CHUNK), 0, 0, img->bytes, sink, ctx };

	stream(&img->src, raw_feed, &a);
	acc_flush(&a);
	free(a.buf);
	return a.produced;
}

static void hash_sink(void *ctx, const uint8_t *data, size_t n, uint64_t offset)
{
	(void)offset;
	SHA256_update(ctx, data, (int)n);
}

static void check_expanded(const struct image *img)
{
	SHA256_CTX c;

	SHA256_init(&c);
	if (expand(img, hash_sink, &c) != img->bytes || memcmp(SHA256_final(&c), img->sha, 32))
		die("expanded %s image differs from the manifest", img->part);
}

struct fit {
	const char *role;
	struct source src;
	uint64_t window_blocks, stage_rel;
	uint8_t *padded;
	uint32_t crc;
};

static void load_fit(struct fit *f, const char *key_prefix)
{
	char key[96];
	uint8_t want[32], got[32];
	uint64_t bytes;

	snprintf(key, sizeof(key), "%s.bytes", key_prefix);
	bytes = mnum(key);
	if (bytes != f->src.size || bytes > f->window_blocks * BLK || bytes < 64)
		die("%s FIT size does not fit its window", f->role);
	f->padded = aligned(f->window_blocks * BLK);
	source_read(&f->src, 0, f->padded, bytes);
	snprintf(key, sizeof(key), "%s.sha256", key_prefix);
	mdigest(key, want);
	SHA256_hash(f->padded, (int)bytes, got);
	if (memcmp(got, want, 32))
		die("%s FIT differs from the manifest", f->role);
	if (memcmp(f->padded, "\xd0\x0d\xfe\xed", 4) ||
	    ((uint64_t)f->padded[4] << 24 | f->padded[5] << 16 | f->padded[6] << 8 | f->padded[7]) > bytes)
		die("%s FIT is not a complete FIT", f->role);
	f->crc = crc32_update(0, f->padded, f->window_blocks * BLK);
}

/* ---- confirmation ---- */

static int confirm(void)
{
	if (confirm_mode >= 0)
		return confirm_mode;
#ifdef CRUX_TEST
	return 0;
#else
	struct pollfd fds[16];
	int n = 0, i, waited;
	DIR *dir = opendir("/dev/input");
	struct dirent *de;

	while (dir && (de = readdir(dir)) && n < 16) {
		char path[300];

		if (strncmp(de->d_name, "event", 5))
			continue;
		snprintf(path, sizeof(path), "/dev/input/%s", de->d_name);
		fds[n].fd = open(path, O_RDONLY | O_NONBLOCK);
		fds[n].events = POLLIN;
		if (fds[n].fd >= 0)
			n++;
	}
	if (dir)
		closedir(dir);
	if (!n)
		return 0;
	for (i = 0; i < n; i++) {
		struct input_event ev;

		while (read(fds[i].fd, &ev, sizeof(ev)) == sizeof(ev))
			;
	}
	ui("Press Volume Up to install, Volume Down to cancel (%d s).", CONFIRM_SECONDS);
	for (waited = 0; waited < CONFIRM_SECONDS * 10; waited++) {
		if (poll(fds, (nfds_t)n, 100) <= 0)
			continue;
		for (i = 0; i < n; i++) {
			struct input_event ev;

			while (read(fds[i].fd, &ev, sizeof(ev)) == sizeof(ev))
				if (ev.type == EV_KEY && ev.value == 1 &&
				    (ev.code == KEY_VOLUMEUP || ev.code == KEY_VOLUMEDOWN))
					return ev.code == KEY_VOLUMEUP;
		}
	}
	return 0;
#endif
}

/* ---- writes ---- */

static int open_volume(const char *name, int flags)
{
	char path[512];
	int fd;

	at(path, sizeof(path), "/dev/block/by-name/%s", name);
	fd = open(path, flags | DIRECT);
	if (fd < 0)
		die("cannot open %s: %s", name, strerror(errno));
#if defined(CRUX_TEST) && defined(F_NOCACHE)
	fcntl(fd, F_NOCACHE, 1);
#endif
	return fd;
}

static void readback(const char *name, uint64_t offset, uint64_t bytes, const uint8_t want[32])
{
	int fd = open_volume(name, O_RDONLY);
	uint8_t *buf = aligned(CHUNK);
	uint64_t done = 0;
	SHA256_CTX c;

	SHA256_init(&c);
	while (done < bytes) {
		size_t n = bytes - done < CHUNK ? (size_t)(bytes - done) : CHUNK;

		read_exact(fd, buf, n, offset + done);
		SHA256_update(&c, buf, (int)n);
		done += n;
	}
	close(fd);
	free(buf);
	if (memcmp(SHA256_final(&c), want, 32))
		die("%s readback differs", name);
}

struct write_ctx {
	int fd;
	uint64_t total;
	double base, span;
};

static void write_sink(void *ctx, const uint8_t *data, size_t n, uint64_t offset)
{
	struct write_ctx *w = ctx;

	write_exact(w->fd, data, n, offset);
	progress(w->base + w->span * (double)(offset + n) / (double)w->total, 0);
}

static void write_image(const struct image *img, double base, double span)
{
	struct write_ctx w = { open_volume(img->part, O_WRONLY), img->bytes, base, span };

	ui("Writing %s (%" PRIu64 " bytes)...", img->part, img->bytes);
	if (expand(img, write_sink, &w) != img->bytes)
		die("%s write length differs", img->part);
	if (fsync(w.fd))
		die("%s sync failed", img->part);
	close(w.fd);
	readback(img->part, 0, img->bytes, img->sha);
	ui("%s written and read back", img->part);
}

static int run_tool(char *const argv[], char *out, size_t n)
{
	int pipe_fd[2], status;
	pid_t pid;
	size_t got = 0;

	if (pipe(pipe_fd))
		die("pipe failed");
	pid = fork();
	if (pid < 0)
		die("fork failed");
	if (!pid) {
		dup2(pipe_fd[1], 1);
		dup2(pipe_fd[1], 2);
		close(pipe_fd[0]);
		execv(argv[0], argv);
		_exit(127);
	}
	close(pipe_fd[1]);
	for (;;) {
		ssize_t r = read(pipe_fd[0], out + got, n - 1 - got);

		if (r <= 0 || got + (size_t)r >= n - 1) {
			if (r > 0)
				got += (size_t)r;
			break;
		}
		got += (size_t)r;
	}
	out[got] = '\0';
	close(pipe_fd[0]);
	if (waitpid(pid, &status, 0) < 0)
		return -1;
	return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static void format_volume(const char *name, uint64_t blocks)
{
	char mke2fs[512], e2fsck[512], tune2fs[512], dev[512], count[32], out[16384], *p;
	int data = !strcmp(name, "peuserdata");
	char *mk_data[] = { mke2fs, "-F", "-t", "ext4", "-b", "4096", "-m", "0", "-I", "512", "-O",
			    "encrypt,quota,project", "-E", "nodiscard,quotatype=usr:grp:prj", dev, count, NULL };
	char *mk_meta[] = { mke2fs, "-F", "-t", "ext4", "-b", "4096", "-m", "0", "-I", "256", "-E",
			    "nodiscard", dev, count, NULL };
	char *fsck[] = { e2fsck, "-fn", dev, NULL };
	char *tune[] = { tune2fs, "-l", dev, NULL };
	uint64_t got_blocks, inode;

	at(mke2fs, sizeof(mke2fs), "/system/bin/mke2fs");
	at(e2fsck, sizeof(e2fsck), "/system/bin/e2fsck");
	at(tune2fs, sizeof(tune2fs), "/system/bin/tune2fs");
	at(dev, sizeof(dev), "/dev/block/by-name/%s", name);
	snprintf(count, sizeof(count), "%" PRIu64, blocks);
	ui("Formatting %s...", name);
	if (run_tool(data ? mk_data : mk_meta, out, sizeof(out)))
		die("mke2fs %s failed: %.200s", name, out);
	if (run_tool(fsck, out, sizeof(out)))
		die("e2fsck %s failed: %.200s", name, out);
	if (run_tool(tune, out, sizeof(out)))
		die("tune2fs %s failed", name);
	p = strstr(out, "Block count:");
	got_blocks = p ? strtoull(p + 12, NULL, 10) : 0;
	p = strstr(out, "Inode size:");
	inode = p ? strtoull(p + 11, NULL, 10) : 0;
	if (got_blocks != blocks || inode != (data ? 512 : 256) || !strstr(out, "has_journal") ||
	    (data && (!strstr(out, "encrypt") || !strstr(out, "project") || !strstr(out, "quota"))))
		die("%s filesystem parameters differ after formatting", name);
	ui("%s formatted and checked", name);
}

static void stage_fit(const struct fit *f)
{
	int fd = open_volume("pesystem", O_WRONLY);
	uint8_t digest[32];
	uint64_t bytes = f->window_blocks * BLK;

	write_exact(fd, f->padded, bytes, f->stage_rel * BLK);
	if (fsync(fd))
		die("pesystem sync failed");
	close(fd);
	SHA256_hash(f->padded, (int)bytes, digest);
	readback("pesystem", f->stage_rel * BLK, bytes, digest);
	ui("%s FIT staged for U-Boot (window CRC %08x)", f->role, f->crc);
}

/* Write a crux-install/1 state block (env -c format) and read it back. */
static void write_state_fields(const char *const *fields)
{
	uint8_t *blob = aligned(BLK), digest[32];
	char *p = (char *)blob + 4;
	uint32_t crc;
	int fd, i;

	for (i = 0; fields[i]; i++)
		p += sprintf(p, "%s", fields[i]) + 1;
	crc = crc32_update(0, blob + 4, BLK - 4);
	blob[0] = (uint8_t)crc;
	blob[1] = (uint8_t)(crc >> 8);
	blob[2] = (uint8_t)(crc >> 16);
	blob[3] = (uint8_t)(crc >> 24);
	fd = open_volume("pesystem", O_WRONLY);
	write_exact(fd, blob, BLK, STATE_REL * BLK);
	if (fsync(fd))
		die("pesystem sync failed");
	close(fd);
	SHA256_hash(blob, (int)BLK, digest);
	readback("pesystem", STATE_REL * BLK, BLK, digest);
	free(blob);
}

static void write_land_state(const char *txn, const struct fit *rec, const struct fit *rom)
{
	char t[80], rc[40], recrc[40];
	const char *fields[] = { "crux_s_ver=1", t, "crux_s_stage=land", "crux_s_tries=0", rc,
				 rec ? "crux_s_rec=1" : NULL, recrc, NULL };

	snprintf(t, sizeof(t), "crux_s_txn=%s", txn);
	snprintf(rc, sizeof(rc), "crux_s_rom_crc=%08x", rom->crc);
	if (rec)
		snprintf(recrc, sizeof(recrc), "crux_s_rec_crc=%08x", rec->crc);
	write_state_fields(fields);
}

/* ---- install ---- */

static void load_image(struct image *img, const char *part, uint64_t limit)
{
	char key[96];
	uint8_t want[32], got[32];

	img->part = part;
	snprintf(key, sizeof(key), "image.%s.entry", part);
	zip_source(mget(key), &img->src);
	snprintf(key, sizeof(key), "image.%s.compression", part);
	if (strcmp(mget(key), "zstd"))
		die("%s image is not zstd", part);
	snprintf(key, sizeof(key), "image.%s.stored_bytes", part);
	if (mnum(key) != img->src.size)
		die("%s stored size differs", part);
	snprintf(key, sizeof(key), "image.%s.stored_sha256", part);
	mdigest(key, want);
	source_sha256(&img->src, got);
	if (memcmp(got, want, 32))
		die("%s differs from the manifest", img->src.name);
	snprintf(key, sizeof(key), "image.%s.bytes", part);
	img->bytes = mnum(key);
	if (!img->bytes || img->bytes % BLK || img->bytes > limit)
		die("%s image size %" PRIu64 " does not fit its volume", part, img->bytes);
	snprintf(key, sizeof(key), "image.%s.sha256", part);
	mdigest(key, img->sha);
	check_expanded(img);
}

static void load_zip_fit(struct fit *f)
{
	char key[96];

	snprintf(key, sizeof(key), "fit.%s.entry", f->role);
	zip_source(mget(key), &f->src);
	snprintf(key, sizeof(key), "fit.%s", f->role);
	load_fit(f, key);
}

#ifndef CRUX_TEST
static void request_reboot(void)
{
	char setprop[512];
	char *argv[] = { setprop, "sys.powerctl", "reboot,recovery", NULL };
	char out[256];

	at(setprop, sizeof(setprop), "/system/bin/setprop");
	ui("Rebooting to the Recovery container U-Boot, which lands both FITs and starts PE.");
	sleep(3);
	if (run_tool(argv, out, sizeof(out)))
		ui("Automatic reboot failed: choose Reboot > Recovery in TWRP to finish.");
}
#endif

static int install(const char *zip)
{
	struct image system_img, vendor_img;
	struct fit rec = { "recovery", { "", -1, 0, 0, NULL }, REC_WINDOW_BLKS, REC_STAGE_REL, NULL, 0 };
	struct fit rom = { "rom", { "", -1, 0, 0, NULL }, ROM_WINDOW_BLKS, ROM_STAGE_REL, NULL, 0 };
	struct source m, s;
	char txn[48], path[512], *text;
	uint8_t sig[64], random[4] = { 0 };
	int fd;

	ui("Crux PE13 installer (crux-install/1)");
	zip_open(zip);
	zip_source("crux-install.txt", &m);
	zip_source("crux-install.sig", &s);
	if (m.size > 16384 || s.size != sizeof(sig))
		die("unexpected manifest or signature size");
	text = malloc(m.size + 1);
	if (!text)
		die("out of memory");
	source_read(&m, 0, text, m.size);
	source_read(&s, 0, sig, sizeof(sig));
	at(path, sizeof(path), "/crux/install-keys/b-p256.pub");
	verify_and_parse(text, m.size, sig, path);
	free(text);
	if (strcmp(mget("schema"), "crux-install/1") || strcmp(mget("kind"), "zip") ||
	    strcmp(mget("device"), "crux") || strcmp(mget("data_policy"), "discard"))
		die("this ZIP is not a crux-install/1 clean-install package");
	ui("Package: %s build %s (signature verified)", mget("product"), mget("target_build"));
	check_device();
	ui("Checking payloads...");
	load_image(&system_img, "pesystem", STATE_REL * BLK);
	load_image(&vendor_img, "pevendor", volumes[1].blocks * BLK);
	load_zip_fit(&rec);
	load_zip_fit(&rom);
	ui("Target: %s", mget("target_fingerprint"));
	ui("Marker: %s", mget("target_marker"));
	ui("Writes pesystem and pevendor, ERASES PE userdata and metadata,");
	ui("stages both PE FITs for U-Boot. Original MIUI partitions are not touched.");
	if (!confirm())
		die("installation cancelled");
	writes_started = 1;
	progress(0.0, 0);
	write_image(&system_img, 0.0, 0.6);
	write_image(&vendor_img, 0.6, 0.25);
	format_volume("peuserdata", volumes[2].blocks);
	format_volume("pemetadata", volumes[3].blocks);
	stage_fit(&rec);
	stage_fit(&rom);
	fd = open("/dev/urandom", O_RDONLY);
	if (fd >= 0) {
		if (read(fd, random, sizeof(random)) != sizeof(random))
			memset(random, 0, sizeof(random));
		close(fd);
	}
	snprintf(txn, sizeof(txn), "z%s-%02x%02x%02x%02x", mget("target_build"),
		 random[0], random[1], random[2], random[3]);
	write_land_state(txn, &rec, &rom);
	progress(1.0, 0);
	ui("Done: transaction %s is ready for U-Boot to land.", txn);
#ifdef CRUX_TEST
	{
		FILE *f;

		at(path, sizeof(path), "/reboot-request");
		f = fopen(path, "w");
		if (f) {
			fprintf(f, "%s\n", txn);
			fclose(f);
		}
	}
#else
	request_reboot();
#endif
	return 0;
}

int main(int argc, char **argv)
{
#ifdef CRUX_TEST
	while (argc > 2 && !strncmp(argv[1], "--", 2)) {
		if (!strcmp(argv[1], "--root"))
			root = argv[2];
		else if (!strcmp(argv[1], "--confirm"))
			confirm_mode = !strcmp(argv[2], "yes");
		argc -= 2;
		argv += 2;
	}
	if (argc != 2)
		return 2;
	return install(argv[1]);
#else
	if (argc != 4)
		return 2;
	ui_pipe = fdopen(atoi(argv[2]), "w");
	return install(argv[3]);
#endif
}
