#include <iostream>
#include <string>
#include "src/metadata_table.h"
#include "src/metadata_label.h"

int main(int argc, char** argv) {
    if (argc < 3) { std::cerr << "usage: check <file> <table>\n"; return 2; }
    MetaDataTable MD;
    try {
        MD.read(argv[1], argv[2]);
    } catch (RelionError& e) {
        std::cerr << "READ FAILED: " << e.msg << "\n";
        return 1;
    }
    std::cout << "OK table=" << argv[2] << " objects=" << MD.numberOfObjects() << "\n";
    return 0;
}
