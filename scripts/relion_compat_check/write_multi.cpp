#include <iostream>
#include "src/metadata_table.h"
#include "src/metadata_label.h"
// Two jobs: RELION switches to loop_ form for multi-row tables.
int main(int argc, char** argv) {
    std::string path = argv[1];
    MetaDataTable gen; gen.setName("pipeline_general"); gen.setIsList(true);
    gen.addObject(); gen.setValue(EMDL_PIPELINE_JOB_COUNTER, 9L);
    MetaDataTable proc; proc.setName("pipeline_processes");
    for (const char* n : {"Tomograms/job007", "Denoise/job008"}) {
        proc.addObject();
        proc.setValue(EMDL_PIPELINE_PROCESS_NAME, std::string(n));
        proc.setValue(EMDL_PIPELINE_PROCESS_ALIAS, std::string("None"));
        proc.setValue(EMDL_PIPELINE_PROCESS_TYPE_LABEL, std::string("relion.reconstructtomograms"));
        proc.setValue(EMDL_PIPELINE_PROCESS_STATUS_LABEL, std::string("Succeeded"));
    }
    // A job.star as RelionJob::write emits it.
    MetaDataTable head; head.setName("job"); head.addObject();
    head.setValue(EMDL_JOB_TYPE_LABEL, std::string("relion.reconstructtomograms"));
    head.setValue(EMDL_JOB_IS_CONTINUE, false);
    head.setValue(EMDL_JOB_IS_TOMO, true);
    MetaDataTable vals; vals.setName("joboptions_values");
    for (auto kv : {std::pair<std::string,std::string>{"in_tiltseries","t.star"},
                    {"xdim","4000"}, {"do_fourier","Yes"}}) {
        vals.addObject();
        vals.setValue(EMDL_JOBOPTION_VARIABLE, kv.first);
        vals.setValue(EMDL_JOBOPTION_VALUE, kv.second);
    }
    std::ofstream fh; fh.open(path);
    gen.write(fh); proc.write(fh); fh.close();
    std::ofstream fh2; fh2.open(path + ".job.star");
    head.write(fh2); vals.write(fh2); fh2.close();
    std::cout << "wrote\n";
    return 0;
}
