#include <cstdio>
#include <string>

#include <android-base/logging.h>

#include "otautil/package.h"
#include "otautil/verifier.h"

// Keep read failures distinct even though verify_file ignores hash-read returns.
class CheckedPackage : public VerifierInterface {
 public:
  explicit CheckedPackage(Package* package) : package_(package) {}
  uint64_t GetPackageSize() const override { return package_->GetPackageSize(); }
  bool ReadFullyAtOffset(uint8_t* buffer, uint64_t count, uint64_t offset) override {
    const bool success = package_->ReadFullyAtOffset(buffer, count, offset);
    io_error_ |= !success;
    return success;
  }
  bool UpdateHashAtOffset(const std::vector<HasherUpdateCallback>& hashers, uint64_t start,
                          uint64_t length) override {
    const bool success = package_->UpdateHashAtOffset(hashers, start, length);
    io_error_ |= !success;
    return success;
  }
  void SetProgress(float progress) override { package_->SetProgress(progress); }
  bool io_error() const { return io_error_; }

 private:
  Package* package_;
  bool io_error_ = false;
};

static int Usage(const char* program, int status) {
  std::fprintf(status == 0 ? stdout : stderr,
               "Usage: %s --package OTA.zip --otacerts otacerts.zip\n"
               "Exit: 0=signature PASS, 1=signature rejected, 2=input/key-load error.\n",
               program);
  return status;
}

int main(int argc, char** argv) {
  android::base::InitLogging(argv, android::base::StderrLogger);
  android::base::SetMinimumLogSeverity(android::base::WARNING);
  if (argc == 2 && std::string(argv[1]) == "--help") {
    return Usage(argv[0], 0);
  }
  if (argc != 5) {
    return Usage(argv[0], 2);
  }

  std::string package_path;
  std::string certificate_zip;
  for (int i = 1; i < argc; i += 2) {
    const std::string option(argv[i]);
    if (option == "--package" && package_path.empty()) {
      package_path = argv[i + 1];
    } else if (option == "--otacerts" && certificate_zip.empty()) {
      certificate_zip = argv[i + 1];
    } else {
      return Usage(argv[0], 2);
    }
  }
  if (package_path.empty() || certificate_zip.empty()) {
    return Usage(argv[0], 2);
  }

  auto keys = LoadKeysFromZipfile(certificate_zip);
  if (keys.empty()) {
    std::fprintf(stderr, "No supported OTA certificates loaded from %s\n",
                 certificate_zip.c_str());
    return 2;
  }
  auto package = Package::CreateFilePackage(package_path, nullptr);
  if (!package) {
    return 2;
  }

  CheckedPackage checked(package.get());
  const int result = verify_file(&checked, keys);
  if (checked.io_error()) {
    std::fprintf(stderr, "Package read error; signature result is not accepted\n");
    return 2;
  }
  std::printf("{\"status\":\"%s\",\"certificate_count\":%zu,"
              "\"package_bytes\":%llu,\"verify_result\":%d}\n",
              result == VERIFY_SUCCESS ? "PASS" : "REJECTED", keys.size(),
              static_cast<unsigned long long>(package->GetPackageSize()), result);
  return result == VERIFY_SUCCESS ? 0 : 1;
}
