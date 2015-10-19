# Copyright (c) 2015, Andrew Delong and Babak Alipanahi All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
# 
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
# 
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation and/or
# other materials provided with the distribution.
# 
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
# 
# Author's note: 
#     This file was distributed as part of the Nature Biotechnology 
#     supplementary software release for DeepBind. Users of DeepBind
#     are encouraged to instead use the latest source code and binaries 
#     for scoring sequences at
#        http://tools.genes.toronto.edu/deepbind/
# 
# predict.py
#    
import os
import os.path
import re
import gc
import glob
import signal
import logging
import traceback
import multiprocessing
import cPickle as cp
import deepity
import warnings
import numpy as np
import smat as sm
from . import globals
from . import util

def load_modelinfos(modeldir, include=None):
    modelinfos = {}

    pfmfiles = glob.glob(os.path.join(modeldir, "*.pfm"))
    if pfmfiles:
        # The directory is full of PFMs, so simply return the list of PFM files
        for pfmfile in pfmfiles:
            match = re.findall('(\w+)_AB\.pfm', os.path.basename(pfmfile))
            if len(match) == 0:
                match = re.findall('([-\w]+)\.pfm', os.path.basename(pfmfile))
            if len(match) == 1:
                modelid = match[0]
                modelinfos[modelid] = { "PFM" : pfmfile }

    else:
        # The directory was generated by Kangaroo, so visit each subdirectory
        # and identify the model it's associated with.
        for dir in os.listdir(modeldir):
            path = os.path.join(modeldir, dir)
            if os.path.isdir(path) and os.path.isfile(os.path.join(path, "model.pkl")):
                modelid = os.path.basename(dir)
                modelinfos[modelid] = { "model" : path }

    # Filter out any model ids that weren't explicitly mention in the "include" list.
    if include:
        # The user has specified to only include a models from a specific list,
        # so parse the list and then filter out the modelids not mentioned.
        if isinstance(include, str):
            if os.path.isfile(include):
                with open(include) as f:
                    include = [line.split("#")[0].rstrip() for line in f.readlines()]  # Ignore comments "# ..."
            else:
                include = include.split(',')

        for id in modelinfos.keys():
            if id not in include:
                modelinfos.pop(id)

    if not modelinfos:
        raise ValueError("Could not find any models that match criteria.")

    return modelinfos


def load_model(modelinfo):
    if "PFM" in modelinfo:
        path = modelinfo["PFM"]
        with open(path) as f:
            f.readline() # Throw away first line
            model = np.asarray([[float(x) for x in line.rstrip().split('\t')[1:]] for line in f.readlines()])
    elif "model" in modelinfo:
        path = modelinfo["model"]
        with open(os.path.join(path, "model.pkl")) as f:
            model = cp.load(f)
    else:
        raise NotImplementedError("Unrecognized modelinfo. Expected it to contain 'PFM' or 'model' value.")
    return model


def gen_pfm_predictionmaps(pfm, data, windowsize=24, want_gmaps=False):
    if len(data.sequencenames) > 1:
        raise ValueError("Cannot apply PFMs to multi-sequence data.")

    predictionmaps = []
    gmaps = []
    nseq = len(data)
    strands = ["fwd", "rev"] if "reverse_complement" in globals.flags else ["fwd"]
    for si in range(nseq):
        pmaps = []
        for strand in strands:
            s = getattr(data, data._seqattrnames(0)[0])[si]
            if strand == "rev":
                s = util.revcomp(s)
            k = len(pfm)
            x = util.acgt2ord(("N"*(k-1)) + s + ("N"*(k-1))).ravel()
            n = len(x)
            idx = np.arange(k)
            pfmN = np.hstack([pfm, 0.25*np.ones((len(pfm),1),pfm.dtype)])
            x = np.minimum(x, 4)
            pmap = np.array([np.prod(pfmN[idx, x[i:i+k]]) for i in range(max(1, n-k+1))], dtype=np.float32)
            if strand == "rev":
                pmap = pmap[::-1]
            pmaps.append(pmap)

        if len(pmaps) > 1:
            pmaps[0] = pmaps[0]+pmaps[1]

        predictionmaps.append(pmaps[0])

    return (predictionmaps, gmaps) if want_gmaps else (predictionmaps, None)

def maskout_revcomp(Z):
    Zmask = None
    if "reverse_complement" in globals.flags:
        Zcols = Z.reshape((-1,2))
        Zmask = np.zeros_like(Zcols,np.bool)
        if globals.flags["reverse_complement"] == "force":
            Zmask[:,0] = False
            Zmask[:,1] = True
            # Hack to visualize TCF7L2/Rfx3 reverse strand on same color scale as forward strand
            #n = len(Zcols)
            #Zmask[0*n/4:1*n/4,0] = True
            #Zmask[1*n/4:2*n/4,1] = True
            #Zmask[2*n/4:3*n/4,0] = True
            #Zmask[3*n/4:4*n/4,1] = True
        else:
            Zmask[:,0] = Zcols[:,0] >= Zcols[:,1]
            Zmask[:,1] = Zcols[:,0] <  Zcols[:,1]
        Zmask = Zmask.reshape((-1,1))
        Z = Z[Zmask.ravel()]
    return Z, Zmask



def gen_convnet_predictions(model, data, want_gmaps=False):
    # We must feed each sequence through the model several times
    # by applying the model repeatedly on sliding a window along the sequence.
    # That generates a prediction map, from which we can take max, sum, etc.
    predictions = []
    gmaps = {}
    batches = data.asbatches(batchsize=2048, reshuffle=False)
    for batch in batches:
        args = batch.input_data()
        args["want_bprop_inputs"] = bool(want_gmaps)
        if isinstance(model.Z.origin().node,deepity.std.softmaxnode):
            args["bprop_inputs_loss"] = deepity.std.nll()
        else:
            args["bprop_inputs_loss"] = deepity.std.mse()
        globals.flags.push("collect_argmax",None)
        outputs = model.eval(**args)
        I = globals.flags.pop("collect_argmax")
        Z = outputs['Z'].asnumpy()
        Z, Zmask = maskout_revcomp(Z)
        if Zmask is not None:
            if "collect_Zmask" in globals.flags:
                global_Zmask = globals.flags.pop("collect_Zmask")
                if not isinstance(global_Zmask,np.ndarray):
                    global_Zmask = Zmask
                else:
                    global_Zmask = np.vstack([global_Zmask, Zmask])
                globals.flags.push("collect_Zmask", global_Zmask)
        predictions.append(Z)

        # If user wants gradientmaps, then for every sequence we need one
        if want_gmaps:
            for key in args:
                dkey = "d"+key
                if outputs.get(dkey,None) is not None:
                    X = args[key].asnumpy()
                    dX = outputs[dkey].asnumpy()
                    if X.dtype == np.uint8: # Is it an sequence of ordinals (bytes)?
                        pad = data.requirements.get("padding",0)
                        R = args["R"+key[1:]].asnumpy() # regions associated with X
                        
                        if want_gmaps == "finite_diff":
                            is_rc = "reverse_complement" in globals.flags
                            #globals.flags.push("force_argmax",I)
                            rcindex = [3,2,1,0]
                            oldF = args['F']
                            # If user specifically asked for finite differences, not instantaneous gradient,
                            # then we need to explicitly mutate every position, generate predictions, and
                            # subtract the result from Z to find the actual delta for each base
                            Xlen = R[:,1]-R[:,0]
                            nbase = dX.shape[1]
                            for i in range(Xlen.max()):
                                for j in range(nbase):
                                    mtX = X.copy()
                                    mtF = args['F'].asnumpy().copy()
                                    for k in range(len(R)):
                                        a,b = R[k]
                                        if i < b-a:
                                            if (k % 2 == 0) or not is_rc:
                                                mtX[pad+a+i] = j  # mutate position i in sequence k (which starts at byte index a) to base j
                                            else:
                                                mtX[b-i-1] = rcindex[j]
                                        mtF[k] = data._generate_dinuc_featurevec(mtX[pad+a:b])
                                        
                                    args[key] = sm.asarray(mtX) # This time use the mutated X instead of the original
                                    args['F'] = sm.asarray(mtF)
                                    mtoutputs = model.eval(**args)
                                    mtZ = mtoutputs['Z'].asnumpy()
                                    mtZ, mtZmask = maskout_revcomp(mtZ)
                                    dZ = mtZ-Z # output 
                                    dZ *= np.maximum(0,np.sign(np.maximum(Z,mtZ)))
                                    for k in range(len(R)):
                                        if (k % 2 == 0) or not is_rc:
                                            a,b = R[k]
                                            if i < b-a:
                                                dX[pad+a+i,j] = dZ[(k//2) if is_rc else k]
                            #globals.flags.pop("force_argmax")
                            args['F'] = oldF
                            
                            # Only include forward strand in finite_diff results
                            if is_rc:
                                dX = [(util.ord2acgt(X[a+pad:b]), dX[a+pad:b]) for a,b in R[np.arange(0,len(R),2)]]
                            else:
                                dX = [(util.ord2acgt(X[a+pad:b]), dX[a+pad:b]) for a,b in R]
                        else:
                            dX = [(util.ord2acgt(X[a+pad:b]), dX[a+pad:b]) for a,b in R]
                            if Zmask is not None:
                                dX = [dX[i] for i in range(len(dX)) if Zmask[i]]

                    else:
                        if Zmask is not None:
                            X = X[Zmask.ravel()]
                            dX = dX[Zmask.ravel()]
                        dX *= np.maximum(0,Z)
                        dX = [(X[i], dX[i]) for i in range(len(dX))]

                    if dkey not in gmaps:
                        gmaps[dkey] = []
                    gmaps[dkey] += dX

    # Concatenate all numpy arrays if they're the same size
    predictions = np.vstack(predictions)

    return (predictions, gmaps) if want_gmaps else (predictions, None)


def gen_convnet_predictionmaps(model, data, stride=1, windowsize=20, want_pmaps=False, want_gmaps=False):
    # We must feed each sequence through the model several times
    # by applying the model repeatedly on sliding a window along the sequence.
    # That generates a prediction map, from which we can take max, sum, etc.
    if len(data.sequencenames) > 1:
        raise ValueError("Cannot currently use --scan on multi-sequence data.")

    # Name of attributes to that we'll be manipulating to generate
    # an artificual chunk of rows for gen_predictions.
    Xname, Rname = data._seqattrnames(0)

    # Each "chunk" will contain raw sequences from a subset of the data.
    # For each of these sequences, set of short sequences will then be generated 
    # by slicing out sliding window from the raw sequence.
    # Those new sequences will then be sent through gen_predictions, and
    # we will take max/avg over appropriate sets of the resulting predictions.
    predictionmaps = []
    gradientmaps = []
    max_chunksize = 32
    nchunk = (len(data) + max_chunksize - 1) // max_chunksize
    for ci in range(nchunk):
        # Slice a our data attributes row-wise, according to chunk index
        chunk = data[ci*max_chunksize:(ci+1)*max_chunksize]
        chunksize = len(chunk)
        rawseqs = [s for s in getattr(chunk, Xname)]

        # Generate a list of subwindows along each sequence in this chunk
        chunk_X = []
        for rawseq in rawseqs:
            padseq = "N"*(windowsize-1) + rawseq + "N"*(windowsize-1)
            chunk_X.append([padseq[i:i+windowsize] for i in range(0,max(1,len(padseq)-windowsize+1), stride)])
        setattr(chunk, Xname, sum(chunk_X,[])) # Append all the sublists into one giant list

        nwindows = [ len(seqlist) for seqlist in chunk_X ]

        for attrname in data.data_attrs() + ("rowidx","foldids","features"):
            if attrname != Xname:
                attr = getattr(chunk, attrname)
                if attr is not None:
                    if isinstance(attr, np.ndarray):
                        setattr(chunk, attrname, np.vstack([np.repeat(attr[i,:], nwindows[i], axis=0).reshape((-1,attr.shape[1])) for i in range(chunksize)]))
                    else:
                        setattr(chunk, attrname, [attr[i] for i in range(chunksize) for j in range(nwindows[i])])

        chunk.sequences = getattr(chunk, Xname)
        chunk.targets   = chunk.Y
        chunk_predictions,chunk_gmaps = gen_convnet_predictions(model, chunk, want_gmaps=want_gmaps)
        chunk_regions = np.cumsum([0]+nwindows)
        for i in range(chunksize):
            # Append a new prediction map. One prediction value per window position.
            # All windows are subsequences of original sequence i.
            pmap = chunk_predictions[chunk_regions[i]:chunk_regions[i+1]].ravel().copy()
            predictionmaps.append(pmap)
            
            if chunk_gmaps is not None:
                # Build a new gradient map. This is done by aligning all the individual window gradient maps,
                # to their position in the original sequence, and taking the average gradientmap value at each location.
                gmap = np.zeros((len(rawseqs[i])+windowsize-1, 4), chunk_predictions[0].dtype)
                denom = np.zeros_like(gmap)
                dX = chunk_gmaps["dX_seq"]  # TODO: this assumes one single sequence attribute called X_seq
                start_window_idx = chunk_regions[i]
                end_window_idx   = chunk_regions[i+1]
                j_pmap = np.argmax(pmap)
                for j in range(0,end_window_idx-start_window_idx):
                    #if j != j_pmap: continue
                    dX_j = dX[start_window_idx+j][1][:min(windowsize, (end_window_idx-start_window_idx-j)*stride)]
                    gmap[ j*stride:j*stride+windowsize] += dX_j
                    denom[j*stride:j*stride+windowsize] += np.ones_like(dX_j)
                gmap /= denom
                gmap = np.nan_to_num(gmap)
                gradientmaps.append(gmap)

    return (predictionmaps, gradientmaps) if want_gmaps else (predictionmaps, None)


_workerid = 0 # This will end up being assigned a different value in each worker process, via the gen_predictions_worker_init function

def is_pfm(model):
    return isinstance(model, np.ndarray)

_predict_worker_inst = None

def _predict_worker_init(devices, global_flags):
    global _predict_worker_inst
    _predict_worker_inst = predict_worker(devices, global_flags)

def _predict_worker_main(params):
    global _predict_worker_inst
    return _predict_worker_inst(params)

def _predict_worker_delete():
    global _predict_worker_inst
    del _predict_worker_inst
    _predict_worker_inst = None
    gc.collect()


class predict_worker(object):

    def __init__(self, devices, global_flags):
        global _workerid
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # Keyboard interrupts go up to main process
        globals.set_devices(devices) # On windows we need to do this because we didn't actually fork
        globals.flags.copy_from(global_flags)

        process = multiprocessing.current_process()
        if process.name == "MainProcess":
            _workerid = 0
        else:
            process_type, process_id = process.name.split("-")         # Get unique 1-base "process_id", which gets larger every time a new pool is created
            _workerid = (int(process_id)-1) % len(devices)                 # Get unique 0-based "worker_id" index, always in range {0,...,nprocess-1}

        # This function is the entry point of a worker process.
        #logging.info("prediction %d on device %d" % (_workerid, globals._devices[_workerid]))
        sm.set_backend_options(device=globals._devices[_workerid])

    def __del__(self):
        process = multiprocessing.current_process()
        if process.name != "MainProcess":
            sm.destroy_backend()

    def __call__(self, params):
        global _workerid
    
        try:
            modelid, modelinfo, scan, stride, data, outdir, verbose, want_pmaps, want_gmaps = params

            # Let the user know what device is working on what modelid
            if verbose:
                print "%d:%s" % (_workerid, modelid)
        
            # Load the first model
            model = load_model(modelinfo)
            if not is_pfm(model):
                data.requirements = model.data_requirements()
            data._reversecomplement = False
            if not data.preprocessors:
                data.load_preprocessors(modelinfo.values()[0])

            predictionmaps = None

            # Generate a prediction for every single sequence in the datasrc
            if is_pfm(model):
                #if want_gmaps:
                #    raise NotImplementedError("gradientmaps not supported for PFM models")
                predictions_direct = np.repeat([[np.nan]], len(data), axis=0)
                pmaps, gmaps = gen_pfm_predictionmaps(model, data, windowsize=scan, want_gmaps=want_gmaps)
            else:
                # Generate "direct" predictions and, if requested, also generate 
                # "prediction maps" by scanning the model
                # along the sequence.
                if scan:
                    predictions_direct, _ = gen_convnet_predictions(model, data)
                    pmaps, gmaps  = gen_convnet_predictionmaps(model, data, windowsize=scan, stride=stride, want_gmaps=want_gmaps)  # each sequence gets a gmap, and each gmap is itself a list, with one entry per window position along the corresponding sequence
                else:
                    assert not want_pmaps, "In direct evaluation mode, it does not make sense to ask for a predictionmap (pmap)"
                    predictions_direct, gmaps = gen_convnet_predictions(model, data, want_gmaps=want_gmaps) # each sequence gets a gmaps, which is just a single array from directly applying the sequence
                    

            if scan or is_pfm(model):
                # In scan mode, we generate several final predictions from the prediction map (take max, take avg etc)
                predictions = {}
                predictions[modelid+".direct"] = predictions_direct
                predictions[modelid+".max"] = np.asarray([np.max(pmap)  for pmap in pmaps])
                predictions[modelid+".avg"] = np.asarray([np.mean(pmap) for pmap in pmaps])
                predictions[modelid+".sum"] = np.asarray([np.sum(pmap)  for pmap in pmaps])
                if want_pmaps:
                    predictions[modelid+".pmaps"] = pmaps
                if want_gmaps:
                    predictions[modelid+".gmaps"] = gmaps
            else:
                # In direct mode, we just report a single prediction
                predictions = { modelid : predictions_direct }
                if want_gmaps:
                    predictions[modelid+".gmaps"] = gmaps

            return predictions

        except Exception as err:
            traceback_str = traceback.format_exc()
            logging.info(err.message + "\n" + traceback_str)    # Do not allow the error to propagate during _call_objective,
            if not globals._allow_multiprocessing:
                raise
            return (err,traceback_str)


def _check_worker_result(result):
    if isinstance(result, tuple) and isinstance(result[0], Exception):
        worker_exception, traceback_str = result
        quit("Error in Worker...\n" + worker_exception.message + "\n" + traceback_str)
    return result


def predict(data, modeldir, outdir, include=None, scan=None, stride=1, verbose=False,
            want_pmaps=False, want_gmaps=False):

    if not isinstance(data,dict):
        data = { id : data.astargets([id]) for id in data.targetnames }

    if include is None:
        include = []
    include = include + data.keys()

    globals._set_default_logging(outdir)

    modelinfos = load_modelinfos(modeldir, include)

    # Generate a process for each device we're allowed to use
    nmodel  = len(modelinfos)
    nworker = len(globals._devices)

    # Each worker is invoked with a model id, path to the model's pickle file, and a datasource with corresponding targets
    workerargs = [(id, modelinfos[id], scan, stride, data[id], outdir, verbose, want_pmaps, want_gmaps) for id in sorted(modelinfos.keys())]
    
    if globals._allow_multiprocessing:
        pool = multiprocessing.Pool(nworker, initializer=_predict_worker_init, initargs=(globals._devices, globals.flags))
        try: 
            predictions = {}
            for worker_predictions in pool.map(_predict_worker_main, workerargs):
                predictions.update(_check_worker_result(worker_predictions))
        except:
            pool.terminate()
            pool.join()
            raise
        else:
            pool.close()
            pool.join()
    else:
        # For interactive debugging
        _predict_worker_init([0],globals.flags)
        predictions = {}
        for workerarg in workerargs:
            predictions.update(_check_worker_result(_predict_worker_main(workerarg)))
        _predict_worker_delete()

    return predictions

