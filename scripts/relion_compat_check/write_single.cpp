#include <iostream>
#include "src/metadata_table.h"
#include "src/metadata_label.h"
// Write a pipeline the way RELION's PipeLine::write does, so the web reader can
// be tested against a file this codebase produced rather than a hand-rolled one.
int main(int argc, char** argv) {
    if (argc < 2) return 2;
    std::string path = argv[1];

    MetaDataTable gen; gen.setName("pipeline_general"); gen.setIsList(true);
    gen.addObject(); gen.setValue(EMDL_PIPELINE_JOB_COUNTER, 7L);

    MetaDataTable proc; proc.setName("pipeline_processes"); proc.setIsList(true);
    proc.addObject();
    proc.setValue(EMDL_PIPELINE_PROCESS_NAME, std::string("Tomograms/job006"));
    proc.setValue(EMDL_PIPELINE_PROCESS_ALIAS, std::string("None"));
    proc.setValue(EMDL_PIPELINE_PROCESS_TYPE_LABEL, std::string("relion.reconstructtomograms"));
    proc.setValue(EMDL_PIPELINE_PROCESS_STATUS_LABEL, std::string("Succeeded"));

    MetaDataTable node; node.setName("pipeline_nodes"); node.setIsList(true);
    node.addObject();
    node.setValue(EMDL_PIPELINE_NODE_NAME, std::string("Tomograms/job006/tomograms.star"));
    node.setValue(EMDL_PIPELINE_NODE_TYPE_LABEL, std::string("TomogramGroupMetadata.star.relion.tomo.reconstruct"));
    node.setValue(EMDL_PIPELINE_NODE_TYPE_DEPTH, 1);
    node.addObject();
    node.setValue(EMDL_PIPELINE_NODE_NAME, std::string("tilt_series.star"));
    node.setValue(EMDL_PIPELINE_NODE_TYPE_LABEL, std::string("TomogramGroupMetadata.star.relion"));
    node.setValue(EMDL_PIPELINE_NODE_TYPE_DEPTH, 1);

    MetaDataTable ein; ein.setName("pipeline_input_edges"); ein.setIsList(true);
    ein.addObject();
    ein.setValue(EMDL_PIPELINE_EDGE_FROM, std::string("tilt_series.star"));
    ein.setValue(EMDL_PIPELINE_EDGE_PROCESS, std::string("Tomograms/job006"));

    MetaDataTable eout; eout.setName("pipeline_output_edges"); eout.setIsList(true);
    eout.addObject();
    eout.setValue(EMDL_PIPELINE_EDGE_PROCESS, std::string("Tomograms/job006"));
    eout.setValue(EMDL_PIPELINE_EDGE_TO, std::string("Tomograms/job006/tomograms.star"));

    std::ofstream fh; fh.open(path);
    gen.write(fh); proc.write(fh); node.write(fh); ein.write(fh); eout.write(fh);
    fh.close();
    std::cout << "wrote " << path << "\n";
    return 0;
}
